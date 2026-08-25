import L from "leaflet";

const PALETTE = {
  idle: { bg: "#c94f3f", fg: "#fff", border: "#fff", ring: "rgba(201, 79, 63, 0.55)" },
  route: { bg: "#a84335", fg: "#fff", border: "#fff", ring: "rgba(168, 67, 53, 0.6)" },
  picked: { bg: "#4f8f43", fg: "#fff", border: "#fff", ring: "rgba(79, 143, 67, 0.65)" },
};

const SIZES = { idle: 42, route: 46, picked: 52 };

function markerHtml(number, variant) {
  const colors = PALETTE[variant] || PALETTE.idle;
  const size = SIZES[variant] || SIZES.idle;
  const fontSize = variant === "picked" ? 16 : variant === "route" ? 15 : 14;

  return `<div style="
    width:${size}px;
    height:${size}px;
    display:grid;
    place-items:center;
    box-sizing:border-box;
    background:${colors.bg};
    color:${colors.fg};
    border:3px solid ${colors.border};
    border-radius:50%;
    font:700 ${fontSize}px/1 'IBM Plex Mono', monospace;
    box-shadow:0 0 0 4px ${colors.ring}, 0 6px 18px rgba(40, 32, 28, 0.35);
  ">${number}</div>`;
}

export function nodeIcon(number, variant) {
  const size = SIZES[variant] || SIZES.idle;
  return L.divIcon({
    className: "knoop-marker",
    html: markerHtml(number, variant),
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
