import type { State } from "../store";

export default function SetupBanner({ state, connected }: { state: State; connected: boolean }) {
  if (!connected) {
    return <div className="banner banner-warn">Reconnecting to the server…</div>;
  }
  if (!state.band.configured) {
    return (
      <div className="banner banner-warn">
        Connect your Band organization: create a user API key (starts with <code>band_u_</code>) at{" "}
        <a href="https://app.band.ai/users/settings" target="_blank" rel="noreferrer">
          app.band.ai/users/settings
        </a>
        , put it in <code>BAND_USER_API_KEY</code> in <code>.env</code> and restart the server.
      </div>
    );
  }
  if (!state.band.online) {
    return (
      <div className="banner banner-error">
        Band rejected the organization key{state.band.error ? `: ${state.band.error}` : "."}
      </div>
    );
  }
  if (!state.anthropicConfigured) {
    return (
      <div className="banner banner-warn">
        Set <code>ANTHROPIC_API_KEY</code> in <code>.env</code> and restart the server so agents can think.
      </div>
    );
  }
  return null;
}
