import { createContext, useContext, useEffect, useState } from "react";
import { useMap, useMapEvents } from "react-leaflet";
import { knoopMarkersVisible, markerScaleForZoom } from "../icons.js";

const MapZoomContext = createContext({
  scale: 1,
  zoom: 13,
  showKnoopMarkers: true,
});

export function useMapZoom() {
  return useContext(MapZoomContext);
}

export function useMarkerScale() {
  return useContext(MapZoomContext).scale;
}

export default function MapZoomScale({ referenceZoom = 13, children }) {
  const map = useMap();
  const [state, setState] = useState(() => {
    const currentZoom = map.getZoom();
    return {
      scale: markerScaleForZoom(currentZoom, referenceZoom),
      zoom: currentZoom,
      showKnoopMarkers: knoopMarkersVisible(currentZoom),
    };
  });

  const sync = () => {
    const currentZoom = map.getZoom();
    setState({
      scale: markerScaleForZoom(currentZoom, referenceZoom),
      zoom: currentZoom,
      showKnoopMarkers: knoopMarkersVisible(currentZoom),
    });
  };

  useMapEvents({
    zoom: sync,
    zoomend: sync,
  });

  useEffect(() => {
    sync();
  }, [map, referenceZoom]);

  return <MapZoomContext.Provider value={state}>{children}</MapZoomContext.Provider>;
}
