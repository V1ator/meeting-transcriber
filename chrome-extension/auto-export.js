(function (root, factory) {
  const api = factory();
  root.MeetingAutoExport = api;
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const LEAVE_CALL_TERMS = [
    "leave call", "leave meeting", "end call",
    "вийти з виклику", "вийти із виклику", "залишити виклик",
    "залишити зустріч", "завершити виклик", "покинути дзвінок",
    "выйти из звонка", "покинуть встречу", "завершить звонок",
    "opuść rozmowę", "opuść spotkanie", "zakończ rozmowę",
    "anruf verlassen", "besprechung verlassen",
    "quitter l'appel", "quitter la réunion",
    "salir de la llamada", "salir de la reunión",
  ];

  function normalize(value) {
    return String(value || "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
  }

  function isLeaveCallLabel(value) {
    const label = normalize(value);
    return LEAVE_CALL_TERMS.some((term) => label.includes(term));
  }

  function findLeaveControl(target) {
    const control = target?.closest?.("button, [role=button]");
    if (!control) return null;
    const label = [
      control.getAttribute("aria-label"),
      control.getAttribute("data-tooltip"),
      control.getAttribute("title"),
      control.textContent,
    ].filter(Boolean).join(" ");
    return isLeaveCallLabel(label) ? control : null;
  }

  function signature(exported) {
    return JSON.stringify({
      meetingCode: exported?.meetingCode || "",
      startedAt: exported?.startedAt || "",
      meetingTitle: exported?.meetingTitle || "",
      // Export lifecycle events may finalize endMs and replace live RTC health
      // with a closed/missing state. Neither change means that the transcript
      // itself changed, so keep the deduplication key semantic and stable.
      entries: (exported?.entries || []).map((entry) => ({
        speaker: entry?.speaker || "",
        text: entry?.text || "",
        ...(entry?.kind ? { kind: entry.kind } : {}),
      })),
      diagnosticOnly: Boolean(exported?.diagnosticOnly),
    });
  }

  function isDiagnosticOnly(exported) {
    const health = exported?.captureHealth || {};
    return !(exported?.entries || []).length
      && Boolean(exported?.diagnosticOnly)
      && (
        Number(health.decodeFailures) > 0
        || Boolean(health.hadRtcUnavailable)
      );
  }

  function shouldExport(exported) {
    return Boolean((exported?.entries || []).length) || isDiagnosticOnly(exported);
  }

  function filename(exported) {
    const code = exported?.meetingCode || "meeting";
    const timestamp = String(exported?.startedAt || new Date().toISOString())
      .slice(0, 19)
      .replace(/[:T]/g, "-");
    return `meet-${code}-${timestamp}.json`;
  }

  return {
    filename,
    findLeaveControl,
    isDiagnosticOnly,
    isLeaveCallLabel,
    normalize,
    shouldExport,
    signature,
  };
});
