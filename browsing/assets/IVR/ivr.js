import { Room } from "./room.js";

const titles = {
  fr: { main: "Votre passerelle Renater", sub: "Audio, vidéo et chat" },
  en: { main: "Votre passerelle Renater", sub: "Audio, vidéo et chat" },
};

const serviceTitle = {
  fr: "Visiby Connect",
  en: "Visiby Connect",
};

const prompts = {
  fr: {
    domain:
      "Veuillez entrer le numéro de la plateforme suivi de #<br>(utilisez * pour corriger)",
    room: {
      jitsi:
        "Veuillez entrer le numéro de votre conférence suivi de #<br>(utilisez * pour corriger)",
      visio:
        "Veuillez entrer le numéro de votre conférence suivi de #<br>(utilisez * pour corriger)",
      teams:
        "Veuillez entrer le numéro de votre conférence suivi de #<br>(utilisez * pour corriger)",
    },
  },
  en: {
    domain:
      "Please enter the platform number followed by #<br>(use * to correct)",
    room: {
      jitsi:
        "Please enter your conference number followed by #<br>(use * to correct)",
      visio:
        "Please enter your conference number followed by #<br>(use * to correct)",
      teams:
        "Please enter your conference number followed by #<br>(use * to correct)",
    },
  },
};

const messages = {
  fr: {
    valid: (code) => `Code validé : ${code}`,
    invalid: (error) => `Le code n'est pas valide ('${error}')`,
    incomplete: (count, expected) =>
      `Le code est trop court (${count}/${expected})`,
    expectedLength: (count, expected) =>
      `Un minimum de ${expected} chiffres (${count}/${expected})`,
    error: (reason) => `Erreur : ${reason}`,
    chosenDomain: (id, name, key) =>
      `
      <div style="display:flex;align-items:center;gap:20px;">
        <span>Plateforme sélectionnée (${id}): ${name}</span> 
        <img style="height:50px;" alt="${name}" src="images/${key}.png" />
      </div>`,
  },
  en: {
    valid: (code) => `Code validated: ${code}`,
    invalid: (error) => `The code is not valid ('${error}')`,
    incomplete: (count, expected) =>
      `The code is too short (${count}/${expected})`,
    expectedLength: (count, expected) =>
      `A minimum of ${expected} digits (${count}/${expected})`,
    error: (reason) => `Error: ${reason}`,
    chosenDomain: (id, name, key) =>
      `
    <div style="display:flex;align-items:center;gap:20px;">
      <span>Platform selected (${id}): ${name}</span> 
      <img style="height:50px;" alt="${name}" src="images/${key}.png" />
    </div>`,
  },
};

document
  .getElementById("digits")
  .addEventListener("beforeinput", (e) => e.preventDefault());

fetch("../config.json")
  .then((res) => res.json())
  .then((config) => initIVR(config))
  .catch((err) => console.error("Failed to load config.json", err));

function initIVR(config) {
  const titleEl = document.getElementById("title");
  const serviceTitleEl = document.getElementById("serviceTitle");
  const messageEl = document.getElementById("message");
  const digitsEl = document.getElementById("digits");
  const statusEl = document.getElementById("status");
  const domainsEl = document.getElementById("domains");
  const digitsLengthMessageEl = document.getElementById("digitsLengthMessage");

  const lang = config["lang"] || "fr";
  const expectedLength = parseInt(config["min_ivr_digit_length"], 10) || 0;
  const domains = parseDomains(config["webrtc_domains"]);

  let inputDigits = [];
  let stage = "domain"; // 'domain' -> 'room'
  let selectedDomain = null;
  let pendingRoomId = null;
  let currentAudio = null;

  function showTitle() {
    titleEl.innerHTML = `<h1>${titles[lang].main}</h1><h2>${titles[lang].sub}</h2>`;
    serviceTitleEl.innerHTML = `<h2>${serviceTitle[lang]}</h2>`;
  }

  function parseDomains(raw) {
    const dict = {};
    let index = 1;
    for (const [key, obj] of Object.entries(raw)) {
      dict[index] = { id: index, key, name: obj.name, domain: obj.domain };
      index++;
    }
    return dict;
  }

  function updateDisplay() {
    const filled = inputDigits.join(" ");
    let empty = "";
    if (stage === "domain") empty = inputDigits.length === 0 ? "_" : "";
    else if (stage === "room")
      empty = Array(Math.max(0, expectedLength - inputDigits.length))
        .fill("_")
        .join(" ");
    digitsEl.textContent = [filled, empty].filter(Boolean).join(" ");
  }

  function showStatus(msg, color) {
    statusEl.innerHTML = msg;
    statusEl.style.color = color ? color : "#3a3a3a";
  }

  function playPromptAudio(type, lang) {
    if (!config.ivr_tts) return;

    if (currentAudio) {
      currentAudio.pause();
      currentAudio.currentTime = 0;
    }
    currentAudio = new Audio(`./${type}_${lang}.mp3`);
    currentAudio.play();
    currentAudio.onended = () => {
      currentAudio = null;
    };
  }

  function showPrompt() {
    if (
      messageEl.innerHTML === prompts?.[lang]?.[stage] ||
      messageEl.innerHTML === prompts?.[lang]?.[stage]?.[selectedDomain?.key]
    )
      return;
    if (stage === "domain" && Object.keys(domains).length > 1) {
      messageEl.innerHTML = prompts[lang].domain;
      domainsEl.classList.remove("hidden");
      domainsEl.innerHTML = Object.values(domains)
        .map(
          (d) =>
            `<div class="domain-item">
              <h3>${d.id} - </h3>
              <div class="domain-logo">
                <img alt="${d.name}" src="images/${d.key}.png"/>
                <div class="domain-name">${d.name}</div>
              </div>
            </div>`
        )
        .join("");
      showTitle();
      playPromptAudio("platform", lang);
    } else {
      messageEl.innerHTML = prompts[lang].room?.[selectedDomain?.key];
      domainsEl.innerHTML = "";
      domainsEl.classList.add("hidden");
      playPromptAudio("conference", lang);
    }
  }

  function handleIncomplete(inputDigits) {
    const msg = messages[lang].expectedLength(
      inputDigits.length,
      expectedLength
    );
    const isLengthOk = inputDigits.length >= expectedLength;
    digitsLengthMessageEl.innerHTML = `<span class="${
      isLengthOk ? "ok" : "nok"
    }" style="color: ${isLengthOk ? "#03bd5b" : "#d63626"}">${msg}</span>`;
  }

  function handleInput(char) {
    if (/^[a-zA-Z0-9-]$/.test(char)) {
      if (stage === "domain") inputDigits = [char];
      else {
        inputDigits.push(char);
        handleIncomplete(inputDigits);
      }
    } else if (char === "*") {
      inputDigits.pop();
      handleIncomplete(inputDigits);
    } else if (char === "#") {
      if (stage === "domain") {
        const domainId = parseInt(inputDigits.join(""), 10);
        if (!isNaN(domainId) && domains[domainId]) {
          selectedDomain = domains[domainId];
          showStatus(
            messages[lang].chosenDomain(
              domainId,
              selectedDomain.name,
              selectedDomain.key
            )
          );
          window.browsing = selectedDomain.key;
          inputDigits = [];
          stage = "room";
          updateDisplay();
          if (pendingRoomId) {
            digitsEl.style.visibility = "hidden";
            (pendingRoomId + "#").split("").forEach(handleInput);
            pendingRoomId = null;
          } else {
            showPrompt();
          }
        } else {
          showStatus(messages[lang].invalid(inputDigits.join("")), "red");
        }
      } else if (stage === "room") {
        const roomId = inputDigits.join("");
        if (expectedLength === 0 || inputDigits.length >= expectedLength) {
          const room = new Room({
            ...config,
            webrtc_domain: selectedDomain.domain,
          });
          room.initRoom(
            roomId,
            () => {
              console.log("Room retrieved successfully:", room.roomName);
              window.room = room;
              digitsLengthMessageEl.style.display = "none";
              digitsEl.style.display = "none";
              messageEl.style.display = "none";
              statusEl.style.display = "none";
              domainsEl.style.display = "none";
              document.removeEventListener("keydown", keyEvent);
            },
            (errorReason) => {
              console.error("Error entering room:", errorReason.error);
              showStatus(messages[lang].invalid(errorReason.error), "red");
              digitsEl.style.visibility = "visible";
              showPrompt();
            }
          );
        } else {
          showStatus(
            messages[lang].incomplete(inputDigits.length, expectedLength),
            "red"
          );
          digitsEl.style.visibility = "visible";
          showPrompt();
        }
      }
      return;
    }
    updateDisplay();
    showStatus("");
  }

  function keyEvent(e) {
    handleInput(e.key);
  }
  document.addEventListener("keydown", keyEvent);

  // Auto-enter from URL
  const urlParams = new URLSearchParams(window.location.search);
  const urlDomainId = urlParams.get("domainId");
  const urlDomainKey = urlParams.get("domainKey");
  const urlRoomId = urlParams.get("roomId");

  if (urlRoomId && urlRoomId !== "0") pendingRoomId = urlRoomId;

  if (urlDomainKey) {
    const found = Object.values(domains).find((d) => d.key === urlDomainKey);
    if (found) {
      selectedDomain = found;
      (selectedDomain.id + "#").split("").forEach(handleInput);
      showStatus(messages[lang].chosenDomain(found.id, found.name, found.key));
    }
  } else if (urlDomainId && domains[urlDomainId]) {
    selectedDomain = domains[urlDomainId];
    (selectedDomain.id + "#").split("").forEach(handleInput);
    console.log(`Auto-selected domain (by id): ${selectedDomain.name}`);
    showStatus(
      messages[lang].chosenDomain(
        urlDomainId,
        selectedDomain.name,
        selectedDomain.key
      )
    );
  }

  if (!selectedDomain && Object.keys(domains).length === 1) {
    selectedDomain = Object.values(domains)[0];
    (selectedDomain.id + "#").split("").forEach(handleInput);
    console.log(`Single domain mode: auto-selected ${selectedDomain.name}`);
  }

  if (!selectedDomain) {
    showPrompt();
    updateDisplay();
  }
}
