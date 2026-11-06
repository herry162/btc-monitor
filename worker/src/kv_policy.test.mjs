import { shouldWriteLastSeen, shouldWriteHeartbeat } from "./index.js";

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

assert(shouldWriteLastSeen({ seeded: true, freshCount: 0 }) === true, "seed should write");
assert(shouldWriteLastSeen({ seeded: false, freshCount: 1 }) === true, "fresh should write");
assert(shouldWriteLastSeen({ seeded: false, freshCount: 0 }) === false, "idle should NOT write");

const hour = 60 * 60 * 1000;
assert(shouldWriteHeartbeat(1000, 0, hour) === true, "never written -> write");
assert(shouldWriteHeartbeat(hour + 1, 1, hour) === true, "past interval -> write");
assert(shouldWriteHeartbeat(hour, 1, hour) === false, "within interval -> skip");
assert(shouldWriteHeartbeat(500, 1, hour) === false, "same window -> skip");

console.log("kv_policy.test.mjs: all passed");
