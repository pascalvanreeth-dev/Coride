import L from "leaflet";

const PALETTE = {
  idle: { bg: "#b83228", fg: "#fff", border: "#fff", ring: "rgba(184, 50, 40, 0.45)" },
  route: { bg: "#b83228", fg: "#fff", border: "#fff", ring: "rgba(184, 50, 40, 0.45)" },
  picked: { bg: "#4f8f43", fg: "#fff", border: "#fff", ring: "rgba(79, 143, 67, 0.5)" },
  start: { bg: "#2563eb", fg: "#fff", border: "#fff", ring: "rgba(37, 99, 235, 0.45)" },
  end: { bg: "#c2410c", fg: "#fff", border: "#fff", ring: "rgba(194, 65, 12, 0.45)" },
};

const SIZES = { idle: 24, route: 24, picked: 28, start: 32, end: 32 };
const FONT_SIZES = { idle: 11, route: 11, picked: 12, start: 12, end: 12 };

/** Knooppunten pas tonen vanaf redelijk ingezoomd niveau. */
export const KNOOP_MARKER_MIN_ZOOM = 11;

export function knoopMarkersVisible(zoom) {
  return Number(zoom) >= KNOOP_MARKER_MIN_ZOOM;
}

/** Schaal knooppunten mee met zoom (1 = referentiezoom). */
export function markerScaleForZoom(zoom, referenceZoom = 13) {
  const scale = 1.14 ** (zoom - referenceZoom);
  return Math.min(1.5, Math.max(0.5, scale));
}

function markerHtml(number, variant, scale = 1) {
  const colors = PALETTE[variant] || PALETTE.route;
  const baseSize = SIZES[variant] || SIZES.route;
  const size = Math.round(baseSize * scale);
  const fontSize = Math.max(8, Math.round((FONT_SIZES[variant] || FONT_SIZES.route) * scale));
  const border = Math.max(1, Math.round(2 * scale));
  const ring = Math.max(1, Math.round(2 * scale));
  const shadow = `0 0 0 ${ring}px ${colors.ring}, 0 ${Math.round(4 * scale)}px ${Math.round(10 * scale)}px rgba(40, 32, 28, 0.28)`;

  return `<div style="
    width:${size}px;
    height:${size}px;
    display:grid;
    place-items:center;
    box-sizing:border-box;
    background:${colors.bg};
    color:${colors.fg};
    border:${border}px solid ${colors.border};
    border-radius:50%;
    font:700 ${fontSize}px/1 'IBM Plex Mono', monospace;
    box-shadow:${shadow};
  ">${number}</div>`;
}

export function nodeIcon(number, variant, scale = 1) {
  const baseSize = SIZES[variant] || SIZES.route;
  const size = Math.round(baseSize * scale);
  return L.divIcon({
    className: "knoop-marker",
    html: markerHtml(number, variant, scale),
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

export const startIcon = L.divIcon({
  className: "start-pin",
  html: `<div class="start-pin">Start</div>`,
  iconSize: [56, 28],
  iconAnchor: [28, 28],
});

/** Sector/categorie voor wens-pictogrammen (SVG, geen emoji). */
export function wishIconCategory(interest, kind, name = "") {
  const i = String(interest || "").toLowerCase().trim();
  const k = String(kind || "").toLowerCase().trim();
  const n = String(name || "").toLowerCase().trim();
  const blob = `${i} ${k} ${n}`;

  // Specifieke soorten eerst.
  if (/café|cafe|cafetje|koffie|coffee|tearoom|theehuis/.test(blob)) return "cafe";
  if (/bakker|bakery|taart|patisserie/.test(blob)) return "bakker";
  if (/brouwer|biergarten|biertuin|pub|tapas/.test(blob) || /\bbar\b/.test(blob)) return "brouwerij";
  if (/restaurant|taverne|brasserie|bistro|eethuis|tafelen|lunch|diner|frituur|snack|ijssalon|ice.?cream/.test(blob)) {
    return "horeca";
  }
  if (i === "horeca" || /horeca/.test(k)) return "horeca";

  if (/museum|musea|galerij|galerie/.test(blob)) return "museum";
  if (/kerk|kapel|kathedraal|abbey|abdij|gebedshuis/.test(blob)) return "kerk";
  if (/kasteel|burcht|manor|herenhuis/.test(blob)) return "kasteel";
  if (/molen|windmill/.test(blob)) return "architectuur";
  if (i === "geschiedenis" || /geschiedenis|historisch|erfgoed|monument|ruïne|archeolog/.test(blob)) {
    return "geschiedenis";
  }
  if (i === "architectuur" || /architectuur/.test(blob)) return "architectuur";
  if (i === "natuur" || /park|bos|natuur|uitzicht|duin|tuin|vegetatie/.test(blob)) return "natuur";
  if (i === "landbouw" || /hoeve|wijn|boerderij|wijngaard|landbouw|streekproduct/.test(blob)) {
    return "landbouw";
  }
  if (i === "oorlog" || /oorlog|memorial|gedenkteken|bunker|fort|slagveld|wo\b/.test(blob)) {
    return "oorlog";
  }
  if (i === "activiteiten" || /speel|attractie|zwem|activiteit/.test(blob)) return "activiteiten";
  if (i === "evenementen" || /evenement|festival|concert/.test(blob)) return "evenementen";
  return "default";
}

function svgWrap(paths, size = 18) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">${paths}</svg>`;
}

const WISH_ICON_PATHS = {
  cafe: `
    <path d="M5 8h11v6.5A3.5 3.5 0 0 1 12.5 18h-2A3.5 3.5 0 0 1 7 14.5V8z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M16 9.5h1.8A2.2 2.2 0 0 1 20 11.7v0A2.2 2.2 0 0 1 17.8 14H16" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M6 20h10" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M9 4.5c.4 1 .4 1.8 0 2.6M12 4c.5 1.1.5 2 0 3" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>`,
  horeca: `
    <path d="M7 3v8M5 3v4.5a2 2 0 0 0 4 0V3M7 11v10" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M16 3c2.2 0 3.5 1.8 3.5 4.2 0 2.2-1.3 3.6-3.5 3.8V21" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M16 3v8" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`,
  brouwerij: `
    <path d="M8 8h8l-.8 11.2a2 2 0 0 1-2 1.8h-2.4a2 2 0 0 1-2-1.8L8 8z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M9 8c0-2 1.2-4 3-4s3 2 3 4" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M9.5 12h5" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>`,
  bakker: `
    <path d="M4.5 14c0-3.2 2.2-6 5.2-6h4.6c3 0 5.2 2.8 5.2 6 0 1.4-1.8 2.5-4 2.5H8.5c-2.2 0-4-1.1-4-2.5z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M8 9.5c.8-1.5 1.8-2.3 3-2.3M13 7.2c1.1 0 2.1.8 2.9 2.2" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>`,
  geschiedenis: `
    <path d="M5 5.5h9.5A2.5 2.5 0 0 1 17 8v11.5H7.5A2.5 2.5 0 0 0 5 22V5.5z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M5 5.5A2.5 2.5 0 0 1 7.5 3H17" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M8.5 9h5.5M8.5 12.5h5.5M8.5 16h3.5" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>`,
  museum: `
    <path d="M4 10.5 12 5l8 5.5" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M6 10.5V18M10 10.5V18M14 10.5V18M18 10.5V18" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M4.5 18h15" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M3.5 20.5h17" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`,
  kerk: `
    <path d="M12 3v3M12 3l-1.2 1.5M12 3l1.2 1.5" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M7 10.5 12 6.5l5 4V20H7v-9.5z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M10.5 20v-4h3v4" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>`,
  kasteel: `
    <path d="M4 20V9l3-2 2 2 3-3 3 3 2-2 3 2v11" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M10 20v-5h4v5" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M4 9h3M17 9h3" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`,
  architectuur: `
    <path d="M4 19h16" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M6 19V10M10 19V10M14 19V10M18 19V10" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M4.5 10h15" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M5 10 12 4l7 6" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>`,
  natuur: `
    <path d="M12 21V11" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M12 14c-3.5 0-6-2.2-6-5.2C6 5.5 9 3.5 12 5c3-1.5 6 .5 6 3.8 0 3-2.5 5.2-6 5.2z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>`,
  landbouw: `
    <path d="M12 21V10" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M12 11c-2.8-1.2-4.5-3.4-4.8-6.2M12 11c2.8-1.2 4.5-3.4 4.8-6.2" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M8.2 8.5c1.2.7 2.4 1 3.8 1M15.8 8.5c-1.2.7-2.4 1-3.8 1" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>
    <path d="M7 21h10" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`,
  oorlog: `
    <path d="M12 3v3" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M7 6h10v2.2c0 3.4-2.1 6.4-5 7.3-2.9-.9-5-3.9-5-7.3V6z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M12 15.5V21M8.5 21h7" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`,
  activiteiten: `
    <circle cx="12" cy="12" r="7.5" stroke="#fff" stroke-width="1.8"/>
    <circle cx="12" cy="12" r="2" fill="#fff"/>
    <path d="M12 4.5v2.2M12 17.3v2.2M4.5 12h2.2M17.3 12h2.2" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>`,
  evenementen: `
    <path d="M6 8.5h12v11H6v-11z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M9 8.5V6.5M15 8.5V6.5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M6 12.5h12" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`,
  default: `
    <path d="M12 3.5 14.2 9.2 20.2 9.7 15.6 13.7 17.1 19.7 12 16.6 6.9 19.7 8.4 13.7 3.8 9.7 9.8 9.2 12 3.5z" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/>`,
};

export function wishPoiSvg(interest, kind, name = "", size = 18) {
  const category = wishIconCategory(interest, kind, name);
  const paths = WISH_ICON_PATHS[category] || WISH_ICON_PATHS.default;
  return svgWrap(paths, size);
}

/** @deprecated alias — gebruik wishPoiSvg; blijft SVG teruggeven voor markers/tegels. */
export function wishPoiGlyph(interest, kind, name = "") {
  return wishPoiSvg(interest, kind, name, 18);
}

export function wishPoiIcon({ interest, kind, name = "", focused = false, selected = false } = {}) {
  const size = focused ? 34 : selected ? 30 : 28;
  const iconSize = Math.round(size * 0.55);
  const bg = selected ? "#4f8f43" : "#c70068";
  const ring = focused
    ? `0 0 0 4px rgba(79, 143, 67, 0.35)`
    : selected
      ? "0 0 0 3px rgba(79, 143, 67, 0.32)"
      : "0 0 0 3px rgba(199, 0, 104, 0.28)";
  const glyph = wishPoiSvg(interest, kind, name, iconSize);
  return L.divIcon({
    className: "wish-poi-marker",
    html: `<div class="wish-poi-marker-inner" style="
      width:${size}px;
      height:${size}px;
      border-radius:50%;
      background:${bg};
      color:#fff;
      border:3px solid #fff;
      box-shadow:${ring}, 0 4px 12px rgba(40,32,28,0.28);
      display:grid;
      place-items:center;
      line-height:0;
    ">${glyph}</div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

export function poiIcon({ selected = false, focused = false } = {}) {
  const bg = focused ? "#2563eb" : selected ? "#4f8f43" : "#c70068";
  const size = focused ? 28 : selected ? 18 : 16;
  const ring = focused ? "0 0 0 3px rgba(37, 99, 235, 0.35)" : "0 2px 8px rgba(40,32,28,0.28)";
  return L.divIcon({
    className: "poi-marker",
    html: `<div style="
      width:${size}px;
      height:${size}px;
      border-radius:4px;
      background:${bg};
      border:2px solid #fff;
      box-shadow:${ring};
    "></div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}
