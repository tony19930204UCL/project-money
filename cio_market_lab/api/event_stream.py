"""Read-only, resumable event transport backed by committed SQLite events.

These endpoints never create signals/orders or synthesize quotes. Historical
BAR_OBSERVED payloads retain their source/freshness fields unaltered.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from cio_market_lab.events.store import EventStore

router=APIRouter(prefix='/api/events',tags=['Paper event observer'])
AUTHORITY='PAPER_AUDIT_OBSERVER_ONLY'
MAX_CURSOR=9223372036854775807


def _cursor(value) -> int:
    text=str(value)
    if not text.isascii() or not text.isdigit():
        raise ValueError('INVALID_EVENT_CURSOR')
    result=int(text)
    if result>MAX_CURSOR:
        raise ValueError('INVALID_EVENT_CURSOR')
    return result


def _store(app) -> EventStore:
    return app.state.app_state.event_store


def _record(sequence, event) -> dict:
    return {'sequence':sequence,'authority':AUTHORITY,'event':event.model_dump(mode='json')}


async def event_frames(store: EventStore, since_id: int=0, poll_seconds: float=0.25,
                       heartbeat_seconds: float=15.0) -> AsyncIterator[str]:
    cursor=_cursor(since_id)
    last_heartbeat=time.monotonic()
    while True:
        # strict prevents database failure from becoming an empty "healthy" stream.
        batch=await asyncio.to_thread(store.get_events,since_id=cursor,limit=100,strict=True)
        for sequence,event in batch:
            data=json.dumps(_record(sequence,event),ensure_ascii=False,separators=(',',':'))
            yield f'id: {sequence}\nevent: {event.event_type.value}\ndata: {data}\n\n'
            cursor=sequence
        if batch:
            last_heartbeat=time.monotonic()
            continue
        if time.monotonic()-last_heartbeat>=heartbeat_seconds:
            yield ': heartbeat\n\n'
            last_heartbeat=time.monotonic()
        await asyncio.sleep(poll_seconds)


@router.get('')
def committed_events(request: Request,
                     since_id: int=Query(0,ge=0,le=MAX_CURSOR),
                     limit: int=Query(100,ge=1,le=1000)):
    rows=_store(request.app).get_events(since_id=since_id,limit=limit,strict=True)
    return {'events':[_record(i,e) for i,e in rows],
            'next_cursor':rows[-1][0] if rows else since_id,'authority':AUTHORITY}


@router.get('/stream')
async def stream(request: Request,since_id: int=Query(0,ge=0,le=MAX_CURSOR)):
    try:
        cursor=_cursor(request.headers.get('last-event-id',since_id))
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    async def body():
        async for frame in event_frames(_store(request.app),cursor):
            if await request.is_disconnected():
                break
            yield frame
    return StreamingResponse(body(),media_type='text/event-stream',
                             headers={'Cache-Control':'no-cache, no-transform',
                                      'X-Accel-Buffering':'no'})


@router.websocket('/ws')
async def websocket_stream(websocket: WebSocket):
    # A browser WebSocket does not use CORS preflight; enforce its Origin here.
    origin=websocket.headers.get('origin')
    if origin:
        parsed=urlsplit(origin)
        expected=urlsplit(str(websocket.url))
        expected_scheme='https' if expected.scheme=='wss' else 'http'
        if parsed.scheme!=expected_scheme or parsed.netloc!=expected.netloc:
            await websocket.close(code=1008,reason='CROSS_ORIGIN_EVENT_STREAM_FORBIDDEN')
            return
    try:
        cursor=_cursor(websocket.query_params.get('since_id','0'))
    except ValueError:
        await websocket.close(code=1008,reason='INVALID_EVENT_CURSOR')
        return
    await websocket.accept()
    last_heartbeat=time.monotonic()
    try:
        while True:
            batch=await asyncio.to_thread(_store(websocket.app).get_events,
                                          since_id=cursor,limit=100,strict=True)
            for sequence,event in batch:
                await websocket.send_json(_record(sequence,event))
                cursor=sequence
            if time.monotonic()-last_heartbeat>=15:
                await websocket.send_json({'type':'heartbeat','cursor':cursor,'authority':AUTHORITY})
                last_heartbeat=time.monotonic()
            if not batch:
                # Consume disconnect/control frames so cancelled readers release promptly.
                try:
                    message=await asyncio.wait_for(websocket.receive(),timeout=0.25)
                    if message.get('type')=='websocket.disconnect':
                        break
                except asyncio.TimeoutError:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close(code=1011,reason='EVENT_STORE_UNAVAILABLE')
        raise
