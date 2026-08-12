(function (root, factory) {
  const api = factory();
  root.MeetingRtcFallback = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function arm(deadline, now, delay, decoded, channelState) {
    if (decoded > 0 || channelState === "open") return 0;
    return deadline > 0 ? deadline : now + delay;
  }

  function isDue(deadline, now, decoded, channelState) {
    return decoded === 0 && channelState !== "open"
      && deadline > 0 && now >= deadline;
  }

  function recovery(domFallbackEnabled) {
    return {
      rtcUnavailable: true,
      domFallbackActive: domFallbackEnabled === true,
      backup: domFallbackEnabled === true ? "dom" : "audio",
    };
  }

  return { arm, isDue, recovery };
});
