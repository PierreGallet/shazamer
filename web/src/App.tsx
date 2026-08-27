import { Show, createResource, createSignal } from "solid-js";
import Analyze from "./components/Analyze";
import Crate from "./components/Crate";
import Library from "./components/Library";
import SetView from "./components/SetView";
import Watches from "./components/Watches";
import { api } from "./lib/api";

type View = "analyze" | "library" | "crate" | "watches" | "set";

export default function App() {
  const [view, setView] = createSignal<View>("analyze");
  const [setId, setSetId] = createSignal<string | null>(null);
  const [sets, { refetch: refetchSets }] = createResource(() => api.sets(60));

  function openSet(id: string) {
    setSetId(id);
    setView("set");
    void refetchSets();
  }

  function go(next: View) {
    setView(next);
    if (next === "library") void refetchSets();
  }

  const navItems: { id: View; label: string }[] = [
    { id: "analyze", label: "Analyse" },
    { id: "library", label: "Library" },
    { id: "crate", label: "Crate" },
    { id: "watches", label: "Following" },
  ];

  return (
    <>
      <header class="app-header">
        <div class="brand">
          <span class="brand-mark">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="#fff">
              <rect x="3" y="10" width="2.4" height="4" rx="1.2" />
              <rect x="8" y="6.5" width="2.4" height="11" rx="1.2" />
              <rect x="13" y="4" width="2.4" height="16" rx="1.2" />
              <rect x="18" y="8.5" width="2.4" height="7" rx="1.2" />
            </svg>
          </span>
          <span class="brand-name">Shazamer</span>
        </div>

        <nav class="nav">
          {navItems.map((item) => (
            <button
              class="nav-item"
              classList={{
                active: view() === item.id || (item.id === "library" && view() === "set"),
              }}
              onClick={() => go(item.id)}
            >
              {item.label}
              <Show when={item.id === "library" && sets()?.length}>
                <span class="nav-count">{sets()!.length}</span>
              </Show>
            </button>
          ))}
        </nav>
      </header>

      <main>
        <Show when={view() === "analyze"}>
          <Analyze onComplete={openSet} />
        </Show>
        <Show when={view() === "library"}>
          <Library onOpen={openSet} />
        </Show>
        <Show when={view() === "crate"}>
          <Crate />
        </Show>
        <Show when={view() === "watches"}>
          <Watches />
        </Show>
        <Show when={view() === "set" && setId()}>
          <SetView setId={setId()!} onBack={() => go("library")} />
        </Show>
      </main>
    </>
  );
}
