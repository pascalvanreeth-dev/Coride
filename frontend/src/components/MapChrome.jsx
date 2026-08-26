import { useEffect, useRef, useState } from "react";
import { geocode } from "../api.js";

function shortPlaceLabel(label) {
  return String(label || "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)[0];
}

export default function MapChrome({
  map,
  onLocate,
  onGoTo,
  locateDisabled = false,
  locateBusy = false,
}) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState([]);
  const panelRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (searchOpen) inputRef.current?.focus();
  }, [searchOpen]);

  useEffect(() => {
    if (!searchOpen) return undefined;
    function onPointerDown(event) {
      if (panelRef.current?.contains(event.target)) return;
      setSearchOpen(false);
      setResults([]);
      setError("");
    }
    function onKeyDown(event) {
      if (event.key === "Escape") {
        setSearchOpen(false);
        setResults([]);
        setError("");
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [searchOpen]);

  function goToHit(hit) {
    const zoom = 12;
    map?.flyTo([hit.lat, hit.lng], zoom, { duration: 0.75 });
    onGoTo?.({ lat: hit.lat, lng: hit.lng, label: hit.label, zoom });
    setSearchOpen(false);
    setQuery("");
    setResults([]);
    setError("");
  }

  async function runSearch(event) {
    event.preventDefault();
    const q = query.trim();
    if (q.length < 2) {
      setError("Typ minstens 2 tekens.");
      setResults([]);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const hits = await geocode(q);
      if (!hits.length) {
        setResults([]);
        setError("Geen plaats in België gevonden. Probeer bv. ‘Knokke-Heist’ of ‘Gent’.");
        return;
      }
      if (hits.length === 1) {
        goToHit(hits[0]);
        return;
      }
      setResults(hits);
    } catch (err) {
      setError(
        err?.message?.includes("fetch")
          ? "Zoeken lukt niet. Controleer of de backend draait op poort 8000."
          : err?.message || "Zoeken mislukt. Probeer opnieuw.",
      );
      setResults([]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="map-chrome" aria-label="Kaartbediening">
      <div className="map-chrome-zoom">
        <button
          type="button"
          className="map-chrome-btn"
          onClick={() => map?.zoomIn()}
          title="Inzoomen"
          aria-label="Inzoomen"
        >
          +
        </button>
        <button
          type="button"
          className="map-chrome-btn"
          onClick={() => map?.zoomOut()}
          title="Uitzoomen"
          aria-label="Uitzoomen"
        >
          −
        </button>
      </div>

      <div className="map-chrome-search" ref={panelRef}>
        <button
          type="button"
          className={`map-chrome-btn map-chrome-search-toggle ${searchOpen ? "on" : ""}`}
          onClick={() => {
            setSearchOpen((open) => !open);
            setResults([]);
            setError("");
          }}
          title="Zoek stad of dorp"
          aria-label="Zoek stad of dorp"
          aria-expanded={searchOpen}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="6.5" />
            <path d="M16.5 16.5L21 21" />
          </svg>
        </button>
        {searchOpen && (
          <div className="map-chrome-search-panel">
            <form className="map-chrome-search-form" onSubmit={runSearch}>
              <input
                ref={inputRef}
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Stad of dorp…"
                aria-label="Zoek een stad of dorp"
                autoComplete="off"
              />
              <button type="submit" className="map-chrome-search-go" disabled={busy}>
                {busy ? "…" : "Ga"}
              </button>
            </form>
            {error && <p className="map-chrome-search-error">{error}</p>}
            {results.length > 0 && (
              <ul className="map-chrome-search-results">
                {results.map((hit) => (
                  <li key={`${hit.lat},${hit.lng},${hit.label}`}>
                    <button type="button" onClick={() => goToHit(hit)}>
                      <strong>{shortPlaceLabel(hit.label)}</strong>
                      <span>{hit.label}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      <button
        type="button"
        className={`map-chrome-btn map-chrome-locate ${locateBusy ? "busy" : ""}`}
        onClick={onLocate}
        disabled={locateDisabled}
        title="Toon mijn locatie"
        aria-label="Toon mijn locatie"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="7" />
          <circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none" />
          <path d="M12 2v4M12 18v4M2 12h4M18 12h4" />
        </svg>
      </button>
    </div>
  );
}
