import { useEffect, useRef } from "react";
import { useMap } from "react-leaflet";

export default function MapFlyTo({ position, trigger = 0, zoom = 15 }) {
  const map = useMap();
  const seen = useRef(0);

  useEffect(() => {
    if (!position || trigger <= seen.current) return;
    const { lat, lng } = position;
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    seen.current = trigger;
    map.flyTo([lat, lng], zoom, { duration: 0.75 });
  }, [map, position, trigger, zoom]);

  return null;
}
