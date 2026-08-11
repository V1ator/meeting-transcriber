(function (root, factory) {
  const api = factory();
  root.MeetingCaptionToggle = api;
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CAPTION_TERMS = [
    "caption", "subtitle", "субтитр", "napis", "untertitel",
    "sous-titre", "sous titre", "subtítulo",
  ];
  const ENABLE_TERMS = [
    "turn on", "enable", "show", " off", "увімк", "показ",
    "включ", "włącz", "pokaż", "einschalten", "anzeigen",
    "activer", "afficher", "activar", "mostrar",
  ];
  const DISABLE_TERMS = [
    "turn off", "disable", "hide", "вимк", "схов", "выключ", "скры",
    "wyłącz", "ukryj", "ausschalten", "ausblenden", "désactiver",
    "masquer", "desactivar", "ocultar",
  ];

  function normalize(value) {
    return String(value || "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
  }

  function isCaptionLabel(value) {
    const label = normalize(value);
    return CAPTION_TERMS.some((term) => label.includes(term));
  }

  function isEnableCaptionLabel(value, ariaPressed) {
    const label = ` ${normalize(value)} `;
    if (!isCaptionLabel(label)) return false;
    if (DISABLE_TERMS.some((term) => label.includes(term))) return false;
    if (ariaPressed === "false") return true;
    return ENABLE_TERMS.some((term) => label.includes(term));
  }

  function findEnableButton(documentRoot) {
    const buttons = Array.from(documentRoot.querySelectorAll("button[aria-label]"));
    return buttons.find((button) => {
      const label = [
        button.getAttribute("aria-label"),
        button.getAttribute("data-tooltip"),
        button.getAttribute("title"),
      ].filter(Boolean).join(" ");
      const visible = button.getClientRects().length > 0;
      return visible && !button.disabled && isEnableCaptionLabel(
        label, button.getAttribute("aria-pressed")
      );
    }) || null;
  }

  return { findEnableButton, isCaptionLabel, isEnableCaptionLabel, normalize };
});
