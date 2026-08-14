(function () {
  "use strict";

  const SETTINGS_KEY = "meeting-transcriber:settings";
  const toggle = document.getElementById("passive-mode");
  const status = document.getElementById("status");

  function showStatus(message, state = "") {
    status.textContent = message;
    status.dataset.state = state;
  }

  async function load() {
    try {
      const stored = await chrome.storage.local.get(SETTINGS_KEY);
      toggle.checked = stored[SETTINGS_KEY]?.passiveDiagnosticMode === true;
      showStatus(
        toggle.checked ? "Пасивний режим увімкнений." : "Звичайний режим увімкнений.",
        "saved"
      );
    } catch {
      toggle.disabled = true;
      showStatus("Не вдалося прочитати налаштування.", "error");
    }
  }

  toggle.addEventListener("change", async () => {
    toggle.disabled = true;
    showStatus("Зберігаю…");
    try {
      const stored = await chrome.storage.local.get(SETTINGS_KEY);
      await chrome.storage.local.set({
        [SETTINGS_KEY]: {
          ...(stored[SETTINGS_KEY] || {}),
          passiveDiagnosticMode: toggle.checked,
        },
      });
      showStatus(
        toggle.checked
          ? "Пасивний режим увімкнений. Перезавантажте Meet."
          : "Звичайний режим увімкнений. Перезавантажте Meet.",
        "saved"
      );
    } catch {
      toggle.checked = !toggle.checked;
      showStatus("Не вдалося зберегти налаштування.", "error");
    } finally {
      toggle.disabled = false;
    }
  });

  load();
})();
