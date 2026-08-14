(function (root, factory) {
  const api = factory();
  root.MeetingCaptionControl = api;
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CAPTION_TERMS = [
    "caption", "subtitle", "subtitul", "субтитр", "napis", "untertitel",
    "sous-titre", "sous titre", "subtítulo", "legenda",
  ];
  const ENABLE_TERMS = [
    "turn on", "enable", "show", "увімк", "включ", "показ",
    "włącz", "pokaż", "einschalten", "anzeigen", "activer", "afficher",
    "activar", "mostrar", "ativar", "exibir",
  ];
  const DISABLE_TERMS = [
    "turn off", "disable", "hide", "вимк", "выключ", "схов", "скры",
    "wyłącz", "ukryj", "ausschalten", "ausblenden", "désactiver",
    "masquer", "desactivar", "ocultar", "desativar", "esconder",
  ];
  const OFF_STATE_TERMS = [
    " are off", " is off", " captions off", " subtitles off",
    "вимкнен", "выключен", "wyłączon", "ausgeschaltet", "désactivé",
    "desactivado", "desativado",
  ];
  const ON_STATE_TERMS = [
    " are on", " is on", " captions on", " subtitles on",
    "увімкнен", "включен", "włączon", "eingeschaltet", "activé",
    "activado", "ativado",
  ];
  const CAPTION_SURFACE_MEDIA_SELECTOR = [
    "video",
    "canvas",
    "[data-participant-id]",
    "[data-requested-participant-id]",
  ].join(", ");
  const CAPTION_SURFACE_INTERACTIVE_SELECTOR = [
    "button",
    "[role=button]",
    "[role=toolbar]",
  ].join(", ");
  const CAPTION_SURFACE_MAX_ANCESTORS = 8;
  const CAPTION_SURFACE_MAX_VIEWPORT_RATIO = 0.45;

  function normalize(value) {
    return String(value || "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
  }

  function controlLabel(control) {
    if (!control) return "";
    return [
      control.getAttribute?.("aria-label"),
      control.getAttribute?.("aria-description"),
      control.getAttribute?.("data-tooltip"),
      control.getAttribute?.("data-tooltip-text"),
      control.getAttribute?.("title"),
      control.textContent,
    ].filter(Boolean).join(" ");
  }

  function isCaptionLabel(value) {
    const label = normalize(value);
    return CAPTION_TERMS.some((term) => label.includes(term));
  }

  function captionState(control) {
    const label = ` ${normalize(controlLabel(control))} `;
    if (!isCaptionLabel(label)) return "unknown";
    if (OFF_STATE_TERMS.some((term) => label.includes(term))) return "disabled";
    if (ON_STATE_TERMS.some((term) => label.includes(term))) return "enabled";
    if (DISABLE_TERMS.some((term) => label.includes(term))) return "enabled";
    if (ENABLE_TERMS.some((term) => label.includes(term))) return "disabled";
    const ariaPressed = control.getAttribute?.("aria-pressed");
    if (ariaPressed === "true") return "enabled";
    if (ariaPressed === "false") return "disabled";
    const ariaChecked = control.getAttribute?.("aria-checked");
    if (ariaChecked === "true") return "enabled";
    if (ariaChecked === "false") return "disabled";
    const isMuted = control.getAttribute?.("data-is-muted");
    if (isMuted === "true") return "disabled";
    if (isMuted === "false") return "enabled";
    const dataState = normalize(control.getAttribute?.("data-state"));
    if (["off", "disabled", "false"].includes(dataState)) return "disabled";
    if (["on", "enabled", "true"].includes(dataState)) return "enabled";
    return "unknown";
  }

  function isVisible(control) {
    if (!control || control.disabled) return false;
    if (control.getAttribute?.("aria-disabled") === "true") return false;
    if (typeof control.getClientRects === "function"
        && control.getClientRects().length === 0) return false;
    return true;
  }

  function candidateControls(documentRoot) {
    if (!documentRoot?.querySelectorAll) return [];
    return Array.from(new Set(documentRoot.querySelectorAll(
      "button[aria-label], button[data-tooltip], button[title], "
      + "button[data-tooltip-text], [role=button][aria-label], "
      + "[role=button][data-tooltip], [role=button][data-tooltip-text], "
      + "[role=button][title]"
    )));
  }

  function captionButtons(documentRoot) {
    return candidateControls(documentRoot).filter((control) => (
      !control.closest?.("#meeting-transcriber-widget")
      && isVisible(control)
      && captionState(control) !== "unknown"
    ));
  }

  function findCaptionButton(documentRoot) {
    return captionButtons(documentRoot)?.[0] || null;
  }

  function findEnableButton(documentRoot) {
    return captionButtons(documentRoot)?.find(
      (button) => captionState(button) === "disabled"
    ) || null;
  }

  function roundedRect(element) {
    const rect = element?.getBoundingClientRect?.();
    if (!rect) return null;
    return Object.fromEntries(
      ["x", "y", "width", "height", "top", "right", "bottom", "left"]
        .map((key) => [key, Math.round(Number(rect[key]) || 0)])
    );
  }

  function layoutNode(element, documentRoot, depth) {
    const style = documentRoot?.defaultView?.getComputedStyle?.(element);
    return {
      depth,
      tag: String(element?.tagName || "").toLocaleLowerCase(),
      role: String(element?.getAttribute?.("role") || ""),
      ariaLabel: String(element?.getAttribute?.("aria-label") || "").slice(0, 160),
      className: String(element?.className || "").slice(0, 300),
      jsname: String(element?.getAttribute?.("jsname") || "").slice(0, 100),
      rect: roundedRect(element),
      style: style ? {
        display: style.display,
        position: style.position,
        opacity: style.opacity,
        visibility: style.visibility,
        height: style.height,
        minHeight: style.minHeight,
        maxHeight: style.maxHeight,
        marginTop: style.marginTop,
        marginBottom: style.marginBottom,
        paddingTop: style.paddingTop,
        paddingBottom: style.paddingBottom,
        overflow: style.overflow,
        flex: style.flex,
        gridTemplateRows: style.gridTemplateRows,
      } : null,
      childCount: Number(element?.children?.length) || 0,
      containsMedia: Boolean(element?.querySelector?.("video, canvas")),
      containsParticipant: Boolean(element?.querySelector?.("[data-participant-id]")),
    };
  }

  function findCaptionVisualSurface(region, documentRoot) {
    if (!region) return null;
    const body = documentRoot?.body;
    const documentElement = documentRoot?.documentElement;
    const viewportHeight = Number(documentRoot?.defaultView?.innerHeight) || 0;
    let surface = region;
    let candidate = region.parentElement;

    for (
      let depth = 1;
      candidate && depth < CAPTION_SURFACE_MAX_ANCESTORS;
      depth += 1
    ) {
      if (candidate === body || candidate === documentElement) break;
      const tag = String(candidate.tagName || "").toLocaleLowerCase();
      const role = normalize(candidate.getAttribute?.("role"));
      if (tag === "main" || role === "main" || role === "dialog") break;
      if (tag === "button" || role === "button" || role === "toolbar") break;
      if (candidate.querySelector?.(CAPTION_SURFACE_MEDIA_SELECTOR)) break;
      const outsideInteractive = Array.from(candidate.querySelectorAll?.(
        CAPTION_SURFACE_INTERACTIVE_SELECTOR
      ) || []).some((element) => !surface.contains?.(element));
      if (outsideInteractive) break;

      const height = Number(candidate.getBoundingClientRect?.().height) || 0;
      if (
        viewportHeight > 0
        && height > viewportHeight * CAPTION_SURFACE_MAX_VIEWPORT_RATIO
      ) break;

      surface = candidate;
      candidate = candidate.parentElement;
    }
    return surface;
  }

  function captionRegionLayouts(documentRoot) {
    if (!documentRoot?.querySelectorAll) return [];
    const regions = Array.from(documentRoot.querySelectorAll(
      '[role="region"][aria-label]'
    )).filter((region) => (
      region.getAttribute("role") === "region"
      && isCaptionLabel(region.getAttribute("aria-label"))
    ));
    return regions.slice(0, 3).map((region) => {
      const chain = [];
      const elements = [];
      let candidate = region;
      for (let depth = 0; candidate && depth < 8; depth += 1) {
        elements.push(candidate);
        chain.push(layoutNode(candidate, documentRoot, depth));
        candidate = candidate.parentElement;
      }
      const visualSurface = findCaptionVisualSurface(region, documentRoot);
      return {
        visualSurfaceDepth: elements.indexOf(visualSurface),
        chain,
      };
    });
  }

  function diagnose(documentRoot) {
    const possible = candidateControls(documentRoot).filter(
      (control) => isCaptionLabel(controlLabel(control))
    );
    return {
      possibleControls: possible.length,
      controls: possible.slice(0, 8).map((control) => ({
        tag: String(control.tagName || "").toLocaleLowerCase(),
        role: String(control.getAttribute?.("role") || ""),
        label: controlLabel(control).slice(0, 300),
        state: captionState(control),
        visible: isVisible(control),
        disabled: Boolean(control.disabled)
          || control.getAttribute?.("aria-disabled") === "true",
        ariaPressed: String(control.getAttribute?.("aria-pressed") || ""),
        ariaChecked: String(control.getAttribute?.("aria-checked") || ""),
        dataIsMuted: String(control.getAttribute?.("data-is-muted") || ""),
        dataState: String(control.getAttribute?.("data-state") || ""),
      })),
      captionRegions: captionRegionLayouts(documentRoot),
    };
  }

  return {
    captionState,
    controlLabel,
    diagnose,
    findCaptionButton,
    findEnableButton,
    findCaptionVisualSurface,
    isCaptionLabel,
    normalize,
  };
});
