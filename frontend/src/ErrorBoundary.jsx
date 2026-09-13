import React from "react";

/** Voorkomt volledig wit scherm bij een render-crash. */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error) {
    console.error("Veloverhaal crash:", error);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="app-error" role="alert" style={{ padding: "2rem", fontFamily: "system-ui" }}>
          <h1>Er ging iets mis</h1>
          <p>Herlaad de pagina of kies opnieuw Plan mijn tocht.</p>
          <button type="button" onClick={() => window.location.reload()}>
            Pagina herladen
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
