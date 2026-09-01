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

export function wishPoiGlyph(interest, kind) {
  const i = String(interest || "").toLowerCase();
  const k = String(kind || "").toLowerCase();
  if (i === "horeca" || /cafe|café|restaurant|pub|bar|eten|lunch|bakker|brouwer/.test(k)) {
    return "🍴";
  }
  if (i === "geschiedenis" || /museum|kasteel|monument|kerk/.test(k)) {
    return "🏛";
  }
  if (i === "natuur" || /park|bos|natuur|uitzicht/.test(k)) {
    return "🌿";
  }
  if (i === "landbouw" || /hoeve|wijn|boerderij/.test(k)) {
    return "🌾";
  }
  if (i === "oorlog" || /memorial|fort|bunker/.test(k)) {
    return "⚑";
  }
  return "★";
}

export function wishPoiIcon({ interest, kind, focused = false, selected = false } = {}) {
  const size = focused ? 34 : selected ? 30 : 28;
  const bg = selected ? "#4f8f43" : "#c70068";
  const ring = focused
    ? `0 0 0 4px rgba(79, 143, 67, 0.35)`
    : selected
      ? "0 0 0 3px rgba(79, 143, 67, 0.32)"
      : "0 0 0 3px rgba(199, 0, 104, 0.28)";
  const glyph = wishPoiGlyph(interest, kind);
  const fontSize = glyph.length > 1 ? Math.round(size * 0.52) : Math.round(size * 0.55);
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
      font:${fontSize}px/1 'IBM Plex Sans', sans-serif;
      line-height:1;
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
