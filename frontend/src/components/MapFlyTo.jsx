import { useEffect, useRef } from "react";
import { useMap } from "react-leaflet";

export const MAP_FLY_PADDING = {
  paddingTopLeft: [20, 20],
  paddingBottomRight: [20, 88],
};

export default function MapFlyTo({ position, trigger = 0, zoom = 15, padding = MAP_FLY_PADDING }) {
  const map = useMap();
  const lastTrigger = useRef(null);

  useEffect(() => {
    if (!position) return;
    const lat = Number(position.lat);
    const lng = Number(position.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    if (trigger === lastTrigger.current) return;
    lastTrigger.current = trigger;
    map.flyTo([lat, lng], zoom, { duration: 0.75, ...padding });
  }, [map, padding, position?.lat, position?.lng, trigger, zoom]);

  return null;
}