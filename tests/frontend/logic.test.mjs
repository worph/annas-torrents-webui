import assert from "node:assert/strict";
import test from "node:test";
import {
  MISSING_API_TOKEN_DETAIL,
  applyAuthFailureCounters,
  applyInvalidateConnectionCounters,
  isAuthTokenMissing503,
  onSavePathChange,
  shouldSkipConnectReconnect,
} from "../../frontend/logic.mjs";

test("only missing API token detail is auth 503", () => {
  assert.equal(isAuthTokenMissing503(MISSING_API_TOKEN_DETAIL), true);
  assert.equal(isAuthTokenMissing503("torrent backend changed"), false);
  assert.equal(isAuthTokenMissing503("too many event connections"), false);
});

test("authFailure counters clear provision and bump request ids", () => {
  const next = applyAuthFailureCounters({
    authBlocked: false,
    provisionInFlight: true,
    connectGeneration: 3,
    storageRequestId: 1,
    spaceRequestId: 2,
    spaceFreeId: 4,
  });
  assert.equal(next.authBlocked, true);
  assert.equal(next.provisionInFlight, false);
  assert.equal(next.connectGeneration, 4);
  assert.equal(next.storageRequestId, 2);
  assert.equal(next.spaceRequestId, 3);
  assert.equal(next.spaceFreeId, 5);
});

test("timeout abort reconnects when generation stable", () => {
  const abort = {};
  assert.equal(
    shouldSkipConnectReconnect({
      generation: 1,
      connectGeneration: 1,
      authBlocked: false,
      statusAbort: abort,
      abort,
    }),
    false
  );
});

test("invalidateConnection abort is ignored", () => {
  const abort = {};
  assert.equal(
    shouldSkipConnectReconnect({
      generation: 1,
      connectGeneration: 1,
      authBlocked: false,
      statusAbort: null,
      abort,
    }),
    true
  );
});

test("save path change invalidates storage and space ids", () => {
  const next = onSavePathChange({
    storageRequestId: 9,
    spaceRequestId: 1,
    spaceFreeId: 2,
  });
  assert.equal(next.storageRequestId, 10);
  assert.equal(next.spaceRequestId, 2);
  assert.equal(next.spaceFreeId, 3);
});

test("invalidateConnection unlocks provision and bumps spaceFreeId", () => {
  const next = applyInvalidateConnectionCounters({
    provisionInFlight: true,
    connectGeneration: 2,
    spaceFreeId: 7,
  });
  assert.equal(next.provisionInFlight, false);
  assert.equal(next.connectGeneration, 3);
  assert.equal(next.spaceFreeId, 8);
});
