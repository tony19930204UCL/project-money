// src/modules-stub/index.ts — open-source fallback for the '@modules'
// manifest. Closed features (AI Agent, future tiered modules) ship only in
// official desktop builds; the public repo builds against this empty
// manifest and <FeatureGate> renders the desktop-only lock screen instead.

import type { ClosedModules } from '../lib/features';

export const closedModules: ClosedModules = {};
