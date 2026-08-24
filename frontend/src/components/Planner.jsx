import { useEffect, useRef, useState } from "react";
import { MapContainer, TileLayer, useMap } from "react-leaflet";
import { reverseGeocode } from "../api.js";
import { getBrowserLocation } from "../geo.js";
import { useDebounced } from "../hooks.js";
import HereMarker from "./HereMarker.jsx";
import "leaflet/dist/leaflet.css";

const PRESETS = [
  { label: "Gent · geschiedenis", start: "Gent", mode: "lus", interests: ["geschiedenis"], distance_km: 22 },
  { label: "Brugge · erfgoedlus", start: "Brugge", mode: "lus", interests: ["geschiedenis", "activiteiten"], distance_km: 18 },
  { label: "Brussel · wat er speelt", start: "Brussel", mode: "lus", interests: ["evenementen", "geschiedenis"], distance_km: 20 },
  { label: "Antwerpen → Mechelen", start: "Antwerpen-Centraal", end: "Mechelen", mode: "punt", interests: ["geschiedenis", "activiteiten"], distance_km: 30 },
];

const INTERESTS = [
  { id: "geschiedenis", label: "Geschiedenis" },
  { id: "activiteiten", label: "Activiteiten" },
  { id: "evenementen", label: "Evenementen" },
];

const LEVELS = [
  { id: "kort", label: "Kort" },
  { id: "normaal", label: "Normaal" },
  { id: "uitgebreid", label: "Uitgebreid" },
];

export default function Planner({ busy, error, center, onPreview, onPlan, geocode }) {
  const [start, setStart] = useState("Gent");
  const [end, setEnd] = useState("");
  const [mode, setMode] = useState("lus");
  const [interests, setInterests] = useState(["geschiedenis"]);
  const [distance, setDistance] = useState(25);
  const [notes, setNotes] = useState("");
  const [level, setLevel] = useState("normaal");
  const [hits, setHits] = useState([]);
  const [here, setHere] = useState(null);
  const [useGpsStart, setUseGpsStart] = useState(false);
  const [geoBusy, setGeoBusy] = useState(false);
  const [geoError, setGeoError] = useState("");
  const skipGeocode = useRef(false);
  const query = useDebounced(start, 350);

  useEffect(() => {
    if (skipGeocode.current) {
      skipGeocode.current = false;
      setHits([]);
      return undefined;
    }
    if (query.length < 3) {
      setHits([]);
      return undefined;
    }
    geocode(query).then(setHits).catch(() => setHits([]));
    return undefined;
  }, [query, geocode]);

  function toggle(id) {
    setInterests((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  function applyPreset(preset) {
    setStart(preset.start);
    setEnd(preset.end || "");
    setMode(preset.mode);
    setInterests(preset.interests);
    setDistance(preset.distance_km);
    setUseGpsStart(false);
  }

  async function useMyLocation() {
    setGeoBusy(true);
    setGeoError("");
    try {
      const next = await getBrowserLocation();
      setHere(next);
      setUseGpsStart(true);
      onPreview({ lat: next.lat, lng: next.lng, zoom: 16 });
      try {
        const hit = await reverseGeocode(next.lat, next.lng);
        skipGeocode.current = true;
        setStart(hit.label);
      } catch {
        skipGeocode.current = true;
        setStart(`${next.lat.toFixed(5)}, ${next.lng.toFixed(5)}`);
      }
    } catch (err) {
      setGeoError(err.message);
    } finally {
      setGeoBusy(false);
    }
  }

  function submit(event) {
    event.preventDefault();
    onPlan({
      start: useGpsStart && here ? `${here.lat.toFixed(5)}, ${here.lng.toFixed(5)}` : start,
      end: mode === "punt" ? end : null,
      mode,
      interests: interests.length ? interests : ["geschiedenis"],
      distance_km: Number(distance),
      notes,
      explanation_level: level,
    });
  }

  return (
    <div className="planner">
      <section className="panel">
        <div>
          <div className="eyebrow">Vlaanderen · knooppunten</div>
          <h1 className="brand">Veloverhaal</h1>
          <p className="lede">
            Een AI-gids plant een lus over het fietsknooppuntennetwerk, toont alle nummers in de
            buurt en vertelt onderweg wat je ziet.
          </p>
        </div>

        <div className="presets">
          {PRESETS.map((preset) => (
            <button key={preset.label} className="chip" type="button" onClick={() => applyPreset(preset)}>
              {preset.label}
            </button>
          ))}
        </div>

        <form onSubmit={submit}>
          <label>
            Startlocatie
            <div className="start-row">
              <input
                value={start}
                onChange={(event) => {
                  setUseGpsStart(false);
                  setStart(event.target.value);
                }}
                required
              />
              <button type="button" className="locate-chip" onClick={useMyLocation} disabled={geoBusy}>
                {geoBusy ? "GPS..." : "Mijn locatie"}
              </button>
            </div>
          </label>
          {geoError && <div className="error">{geoError}</div>}
          {hits.length > 0 && (
            <ul className="suggest">
              {hits.slice(0, 5).map((hit) => (
                <li key={hit.label}>
                  <button
                    type="button"
                    onClick={() => {
                      setStart(hit.label);
                      setHits([]);
                      setUseGpsStart(false);
                      onPreview({ lat: hit.lat, lng: hit.lng, zoom: 12 });
                    }}
                  >
                    {hit.label}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="row">
            <button type="button" className={`mode ${mode === "lus" ? "on" : ""}`} onClick={() => setMode("lus")}>
              Lus
            </button>
            <button type="button" className={`mode ${mode === "punt" ? "on" : ""}`} onClick={() => setMode("punt")}>
              Van A naar B
            </button>
          </div>

          {mode === "punt" && (
            <label>
              Eindlocatie
              <input value={end} onChange={(event) => setEnd(event.target.value)} required={mode === "punt"} />
            </label>
          )}

          {mode === "lus" && (
            <label>
              <span className="range">
                Ongeveer hoe ver? <b>{distance} km</b>
              </span>
              <input
                type="range"
                min="10"
                max="80"
                value={distance}
                onChange={(event) => setDistance(event.target.value)}
              />
            </label>
          )}

          <div>
            <strong>Interesses</strong>
            <div className="row" style={{ marginTop: 8 }}>
              {INTERESTS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`interest ${interests.includes(item.id) ? "on" : ""}`}
                  onClick={() => toggle(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>

          <label>
            Extra wens — de AI past de knooppunten hierop aan
            <textarea
              rows="2"
              placeholder="Bijvoorbeeld: cafés, kastelen, langs het water..."
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </label>

          <div>
            <strong>Hoeveel uitleg per plek?</strong>
            <div className="row" style={{ marginTop: 8 }}>
              {LEVELS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`interest ${level === item.id ? "on" : ""}`}
                  onClick={() => setLevel(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <p className="sources" style={{ margin: "8px 0 0" }}>
              {level === "kort"
                ? "Eén zin bij elke bezienswaardigheid."
                : level === "uitgebreid"
                  ? "Uitgebreid verhaal, met extra details."
                  : "Korte gids-tekst: wat je ziet en waarom het erbij hoort."}
            </p>
          </div>

          {error && <div className="error">{error}</div>}

          <button className="submit" type="submit" disabled={busy}>
            {busy ? "Knooppunten en gids worden gezocht..." : "Plan de tocht"}
          </button>
        </form>

        <p className="sources">
          Kaart: CyclOSM. Route: OSRM. Knooppunten: Toerisme Vlaanderen (gratis WFS). Uitleg:
          Wikipedia + Gemini Flash-Lite.
        </p>
      </section>

      <section className="hero-map">
        <MapContainer center={center} zoom={8} zoomControl={false} attributionControl>
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://www.cyclosm.org">CyclOSM</a>'
            url="https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png"
          />
          <HereMarker position={here} accuracy={here?.accuracy} />
          <Recenter center={center} zoom={here ? 16 : 12} />
        </MapContainer>
        <button className="locate-fab" type="button" onClick={useMyLocation} disabled={geoBusy} title="Toon mijn locatie">
          ⌖
        </button>
        <div className="hero-copy">
          <h2>Volg de nummers. Hoor het verhaal.</h2>
        </div>
      </section>
    </div>
  );
}

function Recenter({ center, zoom }) {
  const map = useMap();
  useEffect(() => {
    map.setView(center, zoom || map.getZoom());
  }, [center, map, zoom]);
  return null;
}
