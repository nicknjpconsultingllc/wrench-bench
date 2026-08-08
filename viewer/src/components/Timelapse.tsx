import { useState } from "react";

/** Schematic timelapse video (one frame per agent step), when the run has
 * one. Renders nothing if the file turns out not to exist — the URL is
 * speculative for fetched runs. */
export function Timelapse({ url }: { url: string }) {
  const [available, setAvailable] = useState(true);
  if (!available) return null;

  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>Timelapse</h2>
          <p className="card-sub">
            one schematic frame per agent step — watch the factory grow, break,
            and recover
          </p>
        </div>
      </div>
      <video
        className="timelapse-video"
        src={url}
        controls
        muted
        loop
        playsInline
        onError={() => setAvailable(false)}
      />
    </section>
  );
}
