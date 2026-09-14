import { Polyline } from "react-leaflet";
import { sanitizeLatLngPositions } from "../geo.js";

export const ROUTE_LINE = {
  color: "#c70068",
  halo: "#ffffff",
  weight: 8,
  haloWeight: 14,
};

const CAP = { lineCap: "round", lineJoin: "round" };

export default function RouteLine({ positions, opacity = 1, dashed = false, color = ROUTE_LINE.color }) {
  const clean = sanitizeLatLngPositions(positions);
  if (clean.length < 2) return null;

  return (
    <>
      <Polyline
        positions={clean}
        pathOptions={{
          color: ROUTE_LINE.halo,
          weight: ROUTE_LINE.haloWeight,
          opacity: opacity * 0.98,
          ...CAP,
        }}
      />
      <Polyline
        positions={clean}
        pathOptions={{
          color,
          weight: ROUTE_LINE.weight,
          opacity,
          dashArray: dashed ? "12 10" : undefined,
          ...CAP,
        }}
      />
    </>
  );
}
