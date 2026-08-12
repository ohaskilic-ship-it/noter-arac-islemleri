
const satisInput = document.getElementById("satisBedeli");
const kaskoInput = document.getElementById("kaskoBedeli");
const yetkiInput = document.getElementById("yetkiBelgesi");
const hesaplaBtn = document.getElementById("hesapla");
const errorBox = document.getElementById("formError");
const resultBox = document.getElementById("sonucAlani");
const openKaskoBtn = document.getElementById("openKasko");
const closeKaskoBtn = document.getElementById("closeKasko");
const modal = document.getElementById("kaskoModal");
const frame = document.getElementById("kaskoFrame");
const selectedKasko = document.getElementById("selectedKasko");
const selectedKaskoCode = document.getElementById("selectedKaskoCode");
const selectedVehicle = document.getElementById("selectedVehicle");
const yeniHesap = document.getElementById("yeniHesap");
const hesaplaLabel = hesaplaBtn.querySelector("span");
const hesaplaArrow = hesaplaBtn.querySelector("b");

function digits(value) {
  return String(value || "").replace(/\D/g, "");
}
function numberValue(input) {
  return Number(digits(input.value)) || 0;
}
function formatInput(input) {
  const value = digits(input.value);
  input.value = value ? Number(value).toLocaleString("tr-TR") : "";
}
function money(value) {
  return new Intl.NumberFormat("tr-TR", {
    style: "currency",
    currency: "TRY",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(Number(value || 0));
}
let kaskoAiValue = null;

[satisInput, kaskoInput].forEach(input => {
  input.addEventListener("input", () => {
    formatInput(input);
    resultBox.classList.add("hidden");
    errorBox.classList.add("hidden");

    if (input === kaskoInput && kaskoAiValue !== null && numberValue(kaskoInput) !== kaskoAiValue) {
      kaskoAiValue = null;
      selectedKasko.classList.add("hidden");
    }
  });
});

function showError(text) {
  errorBox.textContent = text;
  errorBox.classList.remove("hidden");
}

async function calculate() {
  const satis = numberValue(satisInput);
  const kasko = numberValue(kaskoInput);

  if (satis <= 0) {
    showError("Lütfen araç satış bedelini girin.");
    satisInput.focus();
    return;
  }
  if (kasko <= 0) {
    showError("Kasko bedelini girin veya “KaskoAI ile Bul” seçeneğini kullanın.");
    kaskoInput.focus();
    return;
  }

  errorBox.classList.add("hidden");
  hesaplaBtn.disabled = true;
  hesaplaLabel.textContent = "Hesaplanıyor…";
  hesaplaArrow.textContent = "⋯";

  try {
    const response = await fetch("/api/hesapla", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        satis_bedeli: satis,
        kasko_bedeli: kasko,
        yetki_belgesi: yetkiInput.checked
      })
    });
    const data = await response.json();

    if (!response.ok || !data.ok) {
      showError(data.error || "Hesaplama yapılamadı.");
      return;
    }

    document.getElementById("resMatrah").textContent = money(data.matrah);
    document.getElementById("resMatrahKaynagi").textContent = data.matrah_kaynagi || "—";
    document.getElementById("resHarc").textContent = money(data.harc);
    document.getElementById("resNoter").textContent = money(data.noter_ucreti);
    document.getElementById("resArtes").textContent = money(data.artes_tescil);
    document.getElementById("resKdv").textContent = money(data.kdv);
    document.getElementById("resDarphane").textContent = money(data.darphane);
    document.getElementById("resTescil").textContent = money(data.tescil);
    document.getElementById("resToplam").textContent = money(data.genel_toplam);

    document.getElementById("yetkiNote").classList.toggle("hidden", !yetkiInput.checked);
    resultBox.classList.remove("hidden");
    setTimeout(() => resultBox.scrollIntoView({behavior:"smooth", block:"start"}), 50);
  } catch (err) {
    showError("Sunucuya ulaşılamadı. Lütfen tekrar deneyin.");
  } finally {
    hesaplaBtn.disabled = false;
    hesaplaLabel.textContent = "Ücreti Hesapla";
    hesaplaArrow.textContent = "→";
  }
}

hesaplaBtn.addEventListener("click", calculate);
[satisInput, kaskoInput].forEach(input => {
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      calculate();
    }
  });
});
yetkiInput.addEventListener("change", () => resultBox.classList.add("hidden"));

function openKasko() {
  frame.src = `/kasko?picker=1&t=${Date.now()}`;
  modal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}
function closeKasko() {
  modal.classList.add("hidden");
  frame.src = "about:blank";
  document.body.style.overflow = "";
}
openKaskoBtn.addEventListener("click", openKasko);
closeKaskoBtn.addEventListener("click", closeKasko);
modal.addEventListener("click", e => {
  if (e.target === modal) closeKasko();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !modal.classList.contains("hidden")) closeKasko();
});

window.addEventListener("message", event => {
  if (event.origin !== window.location.origin) return;
  const data = event.data || {};
  if (data.type !== "kasko-selected") return;

  const value = Number(data.value || 0);
  if (!value) return;

  kaskoAiValue = value;
  kaskoInput.value = value.toLocaleString("tr-TR");
  selectedKaskoCode.textContent = data.kasko_code || "—";
  selectedVehicle.textContent = [data.year, data.brand, data.vehicle_type].filter(Boolean).join(" ");
  selectedKasko.classList.remove("hidden");
  resultBox.classList.add("hidden");
  closeKasko();
  kaskoInput.dispatchEvent(new Event("change"));
});

yeniHesap.addEventListener("click", () => {
  satisInput.value = "";
  kaskoInput.value = "";
  yetkiInput.checked = false;
  kaskoAiValue = null;
  selectedKasko.classList.add("hidden");
  resultBox.classList.add("hidden");
  errorBox.classList.add("hidden");
  window.scrollTo({top:0, behavior:"smooth"});
  setTimeout(() => satisInput.focus(), 250);
});
