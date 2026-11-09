import { useCallback, useEffect, useMemo, useRef, useState } from "react";

function formatSize(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
}

// Only mount heavy elements (<video>) once a tile scrolls near the viewport.
function useInView() {
  const ref = useRef(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || seen) return;
    const io = new IntersectionObserver(
      ([e]) => {
        if (e.isIntersecting) {
          setSeen(true);
          io.disconnect();
        }
      },
      { rootMargin: "300px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [seen]);
  return [ref, seen];
}

function DownloadLink({ item, className, children }) {
  // The upload script sets Content-Disposition: attachment on every object,
  // so a plain link downloads the untouched original straight from S3.
  return (
    <a
      className={className}
      href={item.url}
      download={item.name}
      onClick={(e) => e.stopPropagation()}
      title={`Download original (${formatSize(item.size)})`}
    >
      {children}
    </a>
  );
}

function Tile({ item, onOpen }) {
  const [ref, seen] = useInView();
  return (
    <div className="tile" ref={ref} onClick={onOpen}>
      {item.type === "image" ? (
        <img src={item.thumb || item.url} loading="lazy" alt={item.name} />
      ) : (
        <>
          {seen && <video src={`${item.url}#t=0.1`} preload="metadata" muted playsInline />}
          <span className="play">▶</span>
        </>
      )}
      <div className="tile-bar">
        <span className="tile-name">{item.name}</span>
        <DownloadLink item={item} className="dl">
          ⬇
        </DownloadLink>
      </div>
    </div>
  );
}

function Lightbox({ items, index, onClose, onIndex }) {
  const item = items[index];
  const [loaded, setLoaded] = useState(false);

  useEffect(() => setLoaded(false), [index]);

  const prev = useCallback(() => onIndex((index - 1 + items.length) % items.length), [index, items.length, onIndex]);
  const next = useCallback(() => onIndex((index + 1) % items.length), [index, items.length, onIndex]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") prev();
      else if (e.key === "ArrowRight") next();
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose, prev, next]);

  return (
    <div className="lightbox" onClick={onClose}>
      <div className="lb-top" onClick={(e) => e.stopPropagation()}>
        <span className="lb-title">
          {item.name} <small>· {formatSize(item.size)} · {index + 1}/{items.length}</small>
        </span>
        <DownloadLink item={item} className="btn">
          Download original
        </DownloadLink>
        <button className="btn ghost" onClick={onClose}>
          ✕
        </button>
      </div>
      <button className="nav left" onClick={(e) => (e.stopPropagation(), prev())}>
        ‹
      </button>
      <button className="nav right" onClick={(e) => (e.stopPropagation(), next())}>
        ›
      </button>
      <div className="lb-media" onClick={(e) => e.stopPropagation()}>
        {item.type === "image" ? (
          <>
            {item.thumb && !loaded && <img className="lb-img blur" src={item.thumb} alt="" />}
            <img
              key={item.key}
              className="lb-img"
              style={{ opacity: loaded ? 1 : 0, position: loaded ? "static" : "absolute" }}
              src={item.url}
              alt={item.name}
              onLoad={() => setLoaded(true)}
            />
            {!loaded && <div className="loading">Loading full quality…</div>}
          </>
        ) : (
          <video key={item.key} className="lb-img" src={item.url} controls autoPlay playsInline />
        )}
      </div>
    </div>
  );
}

export default function App() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [type, setType] = useState("all");
  const [folder, setFolder] = useState("all");
  const [openIndex, setOpenIndex] = useState(null);

  useEffect(() => {
    fetch(`manifest.json?t=${Date.now()}`)
      .then((r) => {
        if (!r.ok) throw new Error(`manifest.json: HTTP ${r.status}`);
        return r.json();
      })
      .then((m) => setItems(m.items))
      .catch((e) => setError(e.message));
  }, []);

  const folders = useMemo(() => [...new Set((items || []).map((i) => i.folder).filter(Boolean))].sort(), [items]);

  const visible = useMemo(
    () => (items || []).filter((i) => (type === "all" || i.type === type) && (folder === "all" || i.folder === folder)),
    [items, type, folder]
  );

  const counts = useMemo(() => {
    const c = { all: 0, image: 0, video: 0 };
    (items || []).forEach((i) => {
      c.all++;
      c[i.type]++;
    });
    return c;
  }, [items]);

  const totalSize = useMemo(() => visible.reduce((s, i) => s + i.size, 0), [visible]);

  return (
    <div className="app">
      <header>
        <h1>Trip Gallery</h1>
        <p className="sub">
          Originals, untouched. Tap anything to view full quality, or hit ⬇ to download the exact file.
        </p>
        <div className="filters">
          {[
            ["all", `All (${counts.all})`],
            ["image", `Photos (${counts.image})`],
            ["video", `Videos (${counts.video})`],
          ].map(([k, label]) => (
            <button key={k} className={`chip ${type === k ? "on" : ""}`} onClick={() => setType(k)}>
              {label}
            </button>
          ))}
          {folders.length > 0 && (
            <select value={folder} onChange={(e) => setFolder(e.target.value)}>
              <option value="all">All folders</option>
              {folders.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          )}
          <span className="total">
            {visible.length} files · {formatSize(totalSize)}
          </span>
        </div>
      </header>

      {error && <p className="msg">Couldn't load the gallery: {error}</p>}
      {!error && items === null && <p className="msg">Loading…</p>}
      {items && items.length === 0 && (
        <p className="msg">
          Nothing here yet. Run <code>python upload.py /path/to/media</code> first.
        </p>
      )}

      <main className="grid">
        {visible.map((item, i) => (
          <Tile key={item.key} item={item} onOpen={() => setOpenIndex(i)} />
        ))}
      </main>

      {openIndex !== null && (
        <Lightbox items={visible} index={openIndex} onIndex={setOpenIndex} onClose={() => setOpenIndex(null)} />
      )}
    </div>
  );
}
