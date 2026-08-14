(function (root, factory) {
  const api = factory();
  root.MeetingRtcFallback = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function isUnhealthy(decoded, channelState, packets = 0, failures = 0) {
    if (decoded > 0) return false;
    if (channelState !== "open") return true;
    return packets > 0 && failures > 0;
  }

  function arm(
    deadline, now, delay, decoded, channelState, packets = 0, failures = 0
  ) {
    if (!isUnhealthy(decoded, channelState, packets, failures)) return 0;
    return deadline > 0 ? deadline : now + delay;
  }

  function isDue(
    deadline, now, decoded, channelState, packets = 0, failures = 0
  ) {
    return isUnhealthy(decoded, channelState, packets, failures)
      && deadline > 0 && now >= deadline;
  }

  return { arm, isDue, isUnhealthy };
});
