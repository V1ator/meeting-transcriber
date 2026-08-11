"use strict";

const HARNESS_STORAGE_KEY = "meeting-transcriber:harness-storage";
const harnessParams = new URLSearchParams(location.search);
if (harnessParams.has("equalVideos")) {
  document.documentElement.classList.add("equal-video-layout");
}
const harnessStorage = JSON.parse(
  localStorage.getItem(HARNESS_STORAGE_KEY) || "{}"
);

function persistHarnessStorage() {
  localStorage.setItem(HARNESS_STORAGE_KEY, JSON.stringify(harnessStorage));
}

globalThis.chrome = {
  runtime: {
    async sendMessage() {},
  },
  storage: {
    local: {
      async get(key) {
        const keys = Array.isArray(key) ? key : [key];
        return Object.fromEntries(
          keys.filter((item) => item in harnessStorage)
            .map((item) => [item, harnessStorage[item]])
        );
      },
      async set(values) {
        Object.assign(harnessStorage, values);
        persistHarnessStorage();
      },
      async remove(key) {
        delete harnessStorage[key];
        persistHarnessStorage();
      },
    },
  },
};

if (harnessParams.has("noStorage")) {
  delete globalThis.chrome.storage;
}

function harnessEntry(speaker, text) {
  const entry = document.createElement("div");
  entry.className = "nMcdL";
  const name = document.createElement("div");
  name.className = "NWpY1d";
  name.textContent = speaker;
  const words = document.createElement("div");
  words.className = "ygicle";
  words.textContent = text;
  entry.append(name, words);
  return entry;
}

const harnessCaptionHost = document.getElementById("caption-host");
const harnessChatHost = document.getElementById("chat-host");
let harnessChatSerial = 1;

function ensureHarnessRegion() {
  let region = document.querySelector('[role="region"]');
  if (!region) {
    region = document.createElement("div");
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", "Captions");
    if (!harnessParams.has("modernRegion")) {
      region.className = "vNKgIf UDinHf";
    }
    const captionMenu = document.createElement("button");
    captionMenu.type = "button";
    captionMenu.textContent = "Caption options";
    region.append(captionMenu);
    harnessCaptionHost.append(region);
  }
  return region;
}

document.getElementById("meet-caption-toggle").addEventListener("click", (event) => {
  ensureHarnessRegion();
  event.currentTarget.setAttribute("aria-label", "Turn off captions (c)");
  event.currentTarget.textContent = "CC увімкнено";
});

document.getElementById("start").addEventListener("click", () => {
  const region = ensureHarnessRegion();
  region.querySelectorAll(".nMcdL").forEach((entry) => entry.remove());
  region.append(harnessEntry("Інтерв’юер", "Перевіряємо"));
});

document.getElementById("extend").addEventListener("click", () => {
  const text = ensureHarnessRegion().querySelector(".ygicle");
  if (text) {
    text.textContent =
      "Перевіряємо українські live captions і захист від повторного додавання історичних реплік після перебудови DOM у Google Meet";
  }
});

document.getElementById("speaker").addEventListener("click", () => {
  ensureHarnessRegion().append(harnessEntry("Анна", "Так, усе працює коректно"));
});

document.getElementById("recreate").addEventListener("click", () => {
  const harnessRegion = ensureHarnessRegion();
  const captions = Array.from(harnessRegion.querySelectorAll(".nMcdL")).map(
    (entry) => ({
      speaker: entry.querySelector(".NWpY1d").textContent,
      text: entry.querySelector(".ygicle").textContent,
    })
  );
  harnessRegion.querySelectorAll(".nMcdL").forEach((entry) => entry.remove());
  harnessRegion.append(
    ...captions.map((caption) => harnessEntry(caption.speaker, caption.text))
  );
});

document.getElementById("chat").addEventListener("click", () => {
  const message = document.createElement("div");
  message.className = "z38b6";
  message.dataset.messageId = `message-${harnessChatSerial++}`;
  const speaker = document.createElement("div");
  speaker.className = "YTbUzc";
  speaker.textContent = "Марія";
  const text = document.createElement("div");
  text.className = "oIy2qc";
  text.textContent = "Питання з чату";
  message.append(speaker, text);
  harnessChatHost.append(message);
});
