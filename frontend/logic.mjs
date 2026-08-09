/** Pure frontend helpers — imported by Node tests; mirrored in app.js behavior. */

export const MISSING_API_TOKEN_DETAIL = "API_TOKEN must be configured";

export function isAuthTokenMissing503(detail) {
  return String(detail || "") === MISSING_API_TOKEN_DETAIL;
}

/** True when connect() should skip reconnect (stale generation or intentional abort). */
export function shouldSkipConnectReconnect({
  generation,
  connectGeneration,
  authBlocked,
  statusAbort,
  abort,
}) {
  if (generation !== connectGeneration || authBlocked) return true;
  // Timeout keeps statusAbort === abort; invalidateConnection clears it.
  if (statusAbort !== abort) return true;
  return false;
}

export function applyAuthFailureCounters(state) {
  return {
    ...state,
    authBlocked: true,
    provisionInFlight: false,
    connectGeneration: (state.connectGeneration || 0) + 1,
    storageRequestId: (state.storageRequestId || 0) + 1,
    spaceRequestId: (state.spaceRequestId || 0) + 1,
    spaceFreeId: (state.spaceFreeId || 0) + 1,
  };
}

/** Settings save / reconnect invalidation — unlock provision, bump free id. */
export function applyInvalidateConnectionCounters(state) {
  return {
    ...state,
    provisionInFlight: false,
    connectGeneration: (state.connectGeneration || 0) + 1,
    spaceFreeId: (state.spaceFreeId || 0) + 1,
  };
}

export function onSavePathChange(state) {
  return {
    ...state,
    storageRequestId: (state.storageRequestId || 0) + 1,
    spaceRequestId: (state.spaceRequestId || 0) + 1,
    spaceFreeId: (state.spaceFreeId || 0) + 1,
  };
}
