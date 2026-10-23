// Stub page. Replaced by Task 9.
export function mount(el) {
  el.replaceChildren();
  const card = document.createElement("section");
  card.className = "card";
  const h1 = document.createElement("h1");
  h1.textContent = "콘티";
  const p = document.createElement("p");
  p.className = "lead";
  p.textContent = "곧 연결됩니다.";
  card.append(h1, p);
  el.append(card);
}
