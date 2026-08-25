import { useEffect } from "react";
import { useMap } from "react-leaflet";

export default function MapResize() {
  const map = useMap();

  useEffect(() => {
    const fix = () => {
      map.invalidateSize({ animate: false, pan: false });
    };
    const timer = window.setTimeout(fix, 0);
    window.addEventListener("resize", fix);
    const parent = map.getContainer()?.parentElement;
    const observer =
      parent && typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(() => fix())
        : null;
    if (observer && parent) observer.observe(parent);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("resize", fix);
      observer?.disconnect();
    };
  }, [map]);

  return null;
}
