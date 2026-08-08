/** Landing drop zone: drag a run directory in, or pick it with the browser. */
import { useRef, useState } from "react";
import { filesFromDrop } from "../lib/loader";

interface DropZoneProps {
  onFiles: (files: File[], dirName: string | null) => void;
  error: string | null;
  loading: boolean;
}

export function DropZone({ onFiles, error, loading }: DropZoneProps) {
  const [dragOver, setDragOver] = useState(false);
  const dirInputRef = useRef<HTMLInputElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const handleDrop = async (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    const { files, dirName } = await filesFromDrop(e.dataTransfer);
    if (files.length > 0) onFiles(files, dirName);
  };

  const handleInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (files.length > 0) onFiles(files, null);
    e.target.value = "";
  };

  return (
    <div
      className={`dropzone${dragOver ? " is-over" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
    >
      <p className="dropzone-title">Drop a WRENCH run directory here</p>
      <p className="dropzone-sub">
        A run directory contains <code>trajectory.jsonl</code>, the disruption ledger
        (<code>&lt;task_key&gt;.jsonl</code>), <code>trajectory.meta.json</code>, and
        optionally <code>samples.json</code>. Everything is parsed in your browser —
        nothing is uploaded.
      </p>
      <div className="dropzone-actions">
        <button
          type="button"
          className="primary-button"
          disabled={loading}
          onClick={() => dirInputRef.current?.click()}
        >
          {loading ? "Loading…" : "Choose run folder"}
        </button>
        <button
          type="button"
          className="ghost-button"
          disabled={loading}
          onClick={() => fileInputRef.current?.click()}
        >
          or select files
        </button>
      </div>
      <p className="dropzone-hint">
        Tip: serve a run next to the app and open <code>?run=runs/&lt;run_dir&gt;</code>.
      </p>
      {error && (
        <p className="dropzone-error" role="alert">
          {error}
        </p>
      )}
      <input
        type="file"
        hidden
        multiple
        ref={(el) => {
          dirInputRef.current = el;
          // Not part of React's typed props; a real attribute on the node.
          el?.setAttribute("webkitdirectory", "");
        }}
        onChange={handleInput}
      />
      <input type="file" hidden multiple ref={fileInputRef} onChange={handleInput} />
    </div>
  );
}
