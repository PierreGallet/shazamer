import { createResource, createRoot, createSignal } from "solid-js";
import { api } from "./api";

/**
 * Sets addressed to you, waiting — one list for the whole app.
 *
 * The nav badge and the "Shared with you" strip read the same resource, so
 * taking or declining a share in the library clears the badge at once
 * instead of on the next page load. Created in its own root because it
 * outlives any one page.
 *
 * Nothing is fetched until `refresh` is first called. The module loads with
 * the app, before anyone has signed in, and a fetch then would answer 401,
 * settle on an empty list and keep it after sign-in.
 */
export const inbox = createRoot(() => {
  const [version, setVersion] = createSignal(0);
  const [shares] = createResource(
    () => version() || false,
    () => api.incomingShares().catch(() => []));
  return { shares, refresh: () => setVersion((n) => n + 1) };
});
