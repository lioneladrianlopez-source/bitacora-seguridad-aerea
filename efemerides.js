"use strict";

const archive = document.querySelector("#efemerides");

if (archive) {
  const argentinaDay = new Intl.DateTimeFormat("en-GB", {
    timeZone: "America/Argentina/Buenos_Aires",
    month: "2-digit",
    day: "2-digit",
  });
  const status = archive.querySelector("#efemerides-hoy");
  const cards = archive.querySelectorAll(".card");

  function updateAnniversaries(now) {
    const parts = argentinaDay.formatToParts(now);
    const month = parts.find(({ type }) => type === "month").value;
    const day = parts.find(({ type }) => type === "day").value;
    const anniversary = month + "-" + day;
    let hasMatches = false;

    for (const card of cards) {
      const date = card.querySelector("time").dateTime;
      const matches = date.slice(5) === anniversary;
      card.querySelector("[data-efemeride-hoy]").hidden = !matches;
      hasMatches ||= matches;
    }

    status.textContent = hasMatches
      ? "Los aniversarios de hoy están señalados en el archivo."
      : "Hoy no hay efemérides registradas.";
    status.hidden = false;
  }

  updateAnniversaries(new Date());
  window.addEventListener("pageshow", () => updateAnniversaries(new Date()));
  window.setInterval(() => updateAnniversaries(new Date()), 60_000);
}

const reading = document.querySelector("#efemeride-lectura");

if (reading && "speechSynthesis" in window && "SpeechSynthesisUtterance" in window) {
  const controls = document.querySelector("#efemeride-controles");
  controls.hidden = false;
  controls.style.display = "";
  const synthesis = window.speechSynthesis;

  document.querySelector("#listen").addEventListener("click", () => {
    const paragraphs = reading.querySelectorAll("h1, h2, p, time");
    const content = Array.from(paragraphs, (part) => part.textContent).join(". ");
    const speech = new SpeechSynthesisUtterance(content);
    speech.lang = "es-AR";
    synthesis.cancel();
    synthesis.speak(speech);
  });

  document.querySelector("#stop").addEventListener("click", () => synthesis.cancel());
  window.addEventListener("pagehide", () => synthesis.cancel());
}
