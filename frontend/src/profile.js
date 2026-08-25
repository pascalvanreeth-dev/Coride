export const STORAGE_KEY = "veloverhaal-profile";

export const AGE_BANDS = [
  { id: "tot18", label: "Tot 18", hint: "School of jeugd" },
  { id: "18-30", label: "18–30", hint: "Jong volwassen" },
  { id: "31-50", label: "31–50", hint: "Meest voorkomend" },
  { id: "51-65", label: "51–65", hint: "Ervaren fietser" },
  { id: "65plus", label: "65+", hint: "Op eigen tempo" },
];

export const FITNESS = [
  { id: "recreant", label: "Rustige recreant", hint: "Korte lussen, vaak stoppen, geen haast." },
  { id: "sportief", label: "Sportief", hint: "Stevige tocht, af en toe een pauze." },
  { id: "wielrenner", label: "Getrainde wielrenner", hint: "Lange dagen, vlot tempo, minder stops." },
];

export const BIKES = [
  { id: "stadsfiets", label: "Stadsfiets", hint: "Comfort, dorpen en jaagpaden." },
  { id: "ebike", label: "E-bike", hint: "Meer kilometers, heuvels zijn geen probleem." },
  { id: "racefiets", label: "Racefiets", hint: "Vlotte wegen, langere etappes." },
  { id: "gravel", label: "Gravel / trekking", hint: "Onverhard, natuur en landbouw." },
];

export const THEMES = [
  { id: "geschiedenis", label: "Geschiedenis", hint: "Kastelen, kerken, musea" },
  { id: "natuur", label: "Natuur & vegetatie", hint: "Parken, bossen, duinen, uitzichten" },
  { id: "landbouw", label: "Landbouw", hint: "Hoeves, wijngaarden, streekproducten" },
  { id: "horeca", label: "Horeca", hint: "Cafés, restaurants, brouwerijen" },
  { id: "oorlog", label: "Oorlog", hint: "Memorialen, forten, WO-erfgoed" },
  { id: "architectuur", label: "Architectuur", hint: "Molens, herenhuizen, kerken" },
  { id: "activiteiten", label: "Activiteiten", hint: "Afstappen, kijken, doen" },
  { id: "evenementen", label: "Evenementen", hint: "Wat er vandaag speelt" },
];

export const HORECA = [
  { id: "snack", label: "Snelle snack", hint: "Frituur, ijs, terras tussendoor" },
  { id: "tafelen", label: "Gezellig tafelen", hint: "Restaurant, lunch of diner" },
  { id: "koffie", label: "Koffie en taart", hint: "Café, bakker, korte stop" },
  { id: "brouwerijen", label: "Lokale brouwerijen", hint: "Brouwerij, pub, biertuin" },
];

export const COMMENTARY = [
  { id: "kort", label: "Beknopt", hint: "Eén zin als je een plek nadert." },
  { id: "normaal", label: "Highlights", hint: "Kort wat het is en waarom het telt." },
  { id: "uitgebreid", label: "Verhalend / gids", hint: "Rijker verhaal, extra details." },
];

export const INTERACTION = [
  { id: "passief", label: "Passief luisteren", hint: "De gids spreekt. Jij fietst." },
  { id: "live", label: "Live vragen stellen", hint: "Je kunt bijvragen over een plek." },
];

export function defaultProfile() {
  return {
    version: 1,
    completed: false,
    ageBand: "31-50",
    fitness: "recreant",
    bike: "stadsfiets",
    interests: ["geschiedenis"],
    horeca: [],
    commentary: "normaal",
    interaction: "live",
  };
}

export function loadProfile() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || parsed.completed !== true) return null;
    return { ...defaultProfile(), ...parsed, completed: true };
  } catch {
    return null;
  }
}

export function saveProfile(profile) {
  const next = { ...defaultProfile(), ...profile, version: 1, completed: true };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  return next;
}

export function interestLabels(ids) {
  return (ids || []).map((id) => THEMES.find((item) => item.id === id)?.label || id);
}

export function mergeInterests(...groups) {
  const merged = [];
  for (const group of groups) {
    for (const item of group || []) {
      if (item && !merged.includes(item)) merged.push(item);
    }
  }
  return merged.length ? merged : ["geschiedenis"];
}

export function suggestedDistance(profile) {
  const fitness = profile?.fitness || "recreant";
  const bike = profile?.bike || "stadsfiets";
  if (fitness === "wielrenner") return bike === "racefiets" ? 55 : 45;
  if (fitness === "sportief") return bike === "ebike" ? 40 : 32;
  if (bike === "ebike") return 28;
  if (bike === "stadsfiets") return 18;
  return 25;
}

export function suggestedMinutes(profile) {
  const km = suggestedDistance(profile);
  const fitness = profile?.fitness || "recreant";
  const bike = profile?.bike || "stadsfiets";
  const speed =
    {
      recreant: { stadsfiets: 14, ebike: 18, racefiets: 18, gravel: 15 },
      sportief: { stadsfiets: 17, ebike: 22, racefiets: 24, gravel: 19 },
      wielrenner: { stadsfiets: 20, ebike: 24, racefiets: 28, gravel: 22 },
    }[fitness]?.[bike] || 16;
  return Math.round((km / speed) * 60);
}

export function profileSummary(profile) {
  if (!profile) return "";
  const fitness = FITNESS.find((item) => item.id === profile.fitness)?.label || profile.fitness;
  const bike = BIKES.find((item) => item.id === profile.bike)?.label || profile.bike;
  const n = profile.interests?.length || 0;
  return `${fitness} · ${bike} · ${n} interesse${n === 1 ? "" : "s"}`;
}

export function toApiProfile(profile) {
  if (!profile) return null;
  return {
    age_band: profile.ageBand,
    fitness: profile.fitness,
    bike: profile.bike,
    horeca: profile.horeca || [],
    commentary: profile.commentary,
    interaction: profile.interaction,
  };
}
