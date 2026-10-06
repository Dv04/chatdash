// One rule for "is this chat stuck on a limit", shared by every list that draws a chat's state (board list, Chats table, chat header,
// graph, sky panel, work item). A chat with a limit banner, or a working chat on a seat that is at its limit, is not working: it waits
// for a reset or a Continue on another seat.
export function seatsAtLimit(ov, graph) {
  const s = new Set();
  for (const x of (ov && ov.capacity && ov.capacity.seats) || []) if (x.state === "blocked") s.add(x.seat);
  for (const n of (graph && graph.nodes) || []) if (n.type === "seat" && n.state === "blocked") s.add(n.seat);
  return s;
}
export function limitWord(c, blocked) {
  if (c.limited) return "at a limit";
  if (blocked && blocked.has(c.seat) && c.live !== false && (c.state === "working" || c.needs_you)) return "seat at its limit";
  return null;
}
