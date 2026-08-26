import { useEffect } from "react";
import { useMap } from "react-leaflet";

/** Geeft de Leaflet-mapinstantie door aan de vaste schermcontrols. */
export default function MapReady({ onReady }) {
  const map = useMap();

  useEffect(() => {
    onReady?.(map);
    return () => onReady?.(null);
  }, [map, onReady]);

  return null;
}
