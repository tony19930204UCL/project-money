// src/lib/market-session.ts
// Market session calculation for TW and US markets

export interface MarketSessionStatus {
    market: 'TW' | 'US';
    status: 'OPEN' | 'PRE_MARKET' | 'POST_MARKET' | 'NIGHT_SESSION' | 'CLOSED' | 'WEEKEND';
    label: string;
    sublabel: string;
    isOpen: boolean;
    timezone: string;
}

export function getTwSession(date: Date = new Date()): MarketSessionStatus {
    // Taiwan is UTC+8
    const utc = date.getTime() + date.getTimezoneOffset() * 60000;
    const twDate = new Date(utc + 3600000 * 8);

    const day = twDate.getDay(); // 0 is Sunday, 6 is Saturday
    const hours = twDate.getHours();
    const minutes = twDate.getMinutes();
    const timeNum = hours * 100 + minutes;

    if (day === 0 || day === 6) {
        return {
            market: 'TW',
            status: 'WEEKEND',
            label: '台股休市 (週末)',
            sublabel: '週一 09:00 開盤',
            isOpen: false,
            timezone: 'Asia/Taipei (UTC+8)',
        };
    }

    if (timeNum >= 830 && timeNum < 900) {
        return {
            market: 'TW',
            status: 'PRE_MARKET',
            label: '台股試撮 (08:30–09:00)',
            sublabel: '盤前試撮進行中',
            isOpen: true,
            timezone: 'Asia/Taipei (UTC+8)',
        };
    }

    if (timeNum >= 900 && timeNum < 1330) {
        return {
            market: 'TW',
            status: 'OPEN',
            label: '台股常規 (09:00–13:30)',
            sublabel: '盤中即時撮合',
            isOpen: true,
            timezone: 'Asia/Taipei (UTC+8)',
        };
    }

    if (timeNum >= 1330 && timeNum < 1430) {
        return {
            market: 'TW',
            status: 'POST_MARKET',
            label: '台股盤後 (13:30–14:30)',
            sublabel: '盤後定價交易',
            isOpen: true,
            timezone: 'Asia/Taipei (UTC+8)',
        };
    }

    if (timeNum >= 1500 || timeNum < 500) {
        return {
            market: 'TW',
            status: 'NIGHT_SESSION',
            label: '台指夜盤 (15:00–05:00)',
            sublabel: '期貨夜盤交易',
            isOpen: true,
            timezone: 'Asia/Taipei (UTC+8)',
        };
    }

    return {
        market: 'TW',
        status: 'CLOSED',
        label: '台股收盤 (Closed)',
        sublabel: '今日常規交易已結束',
        isOpen: false,
        timezone: 'Asia/Taipei (UTC+8)',
    };
}

export function getUsSession(date: Date = new Date()): MarketSessionStatus {
    // US Eastern Time: EDT (UTC-4) / EST (UTC-5)
    // Approximate with US Eastern Time using Intl or standard offset
    let etDate: Date;
    try {
        const etString = date.toLocaleString('en-US', { timeZone: 'America/New_York' });
        etDate = new Date(etString);
    } catch {
        // Fallback EDT UTC-4
        const utc = date.getTime() + date.getTimezoneOffset() * 60000;
        etDate = new Date(utc - 3600000 * 4);
    }

    const day = etDate.getDay();
    const hours = etDate.getHours();
    const minutes = etDate.getMinutes();
    const timeNum = hours * 100 + minutes;

    if (day === 0 || day === 6) {
        return {
            market: 'US',
            status: 'WEEKEND',
            label: '美股休市 (Weekend)',
            sublabel: '週一 09:30 ET 開盤',
            isOpen: false,
            timezone: 'America/New_York (ET)',
        };
    }

    if (timeNum >= 400 && timeNum < 930) {
        return {
            market: 'US',
            status: 'PRE_MARKET',
            label: '美股盤前 (04:00–09:30 ET)',
            sublabel: 'Pre-market session',
            isOpen: true,
            timezone: 'America/New_York (ET)',
        };
    }

    if (timeNum >= 930 && timeNum < 1600) {
        return {
            market: 'US',
            status: 'OPEN',
            label: '美股常規 (09:30–16:00 ET)',
            sublabel: 'Regular Trading Hours',
            isOpen: true,
            timezone: 'America/New_York (ET)',
        };
    }

    if (timeNum >= 1600 && timeNum < 2000) {
        return {
            market: 'US',
            status: 'POST_MARKET',
            label: '美股盤後 (16:00–20:00 ET)',
            sublabel: 'After-hours session',
            isOpen: true,
            timezone: 'America/New_York (ET)',
        };
    }

    return {
        market: 'US',
        status: 'CLOSED',
        label: '美股收盤 (Closed)',
        sublabel: 'Next session 04:00 ET',
        isOpen: false,
        timezone: 'America/New_York (ET)',
    };
}
