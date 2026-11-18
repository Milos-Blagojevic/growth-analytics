(() => {
  function openDisclosure(hash = location.hash) {
    const target = document.getElementById(hash.slice(1));
    if (target?.tagName === 'DETAILS') target.open = true;
  }
  document.querySelectorAll('.topbar a[href^="#"]').forEach(link => {
    link.addEventListener('click', () => openDisclosure(link.getAttribute('href')));
  });
  window.addEventListener('hashchange', () => openDisclosure());
  openDisclosure();

  const nodes = [...document.querySelectorAll('[data-map-node]')];
  const panels = [...document.querySelectorAll('[data-panel]')];
  if (!nodes.length) return;

  const map = document.getElementById('map-overview');
  const wires = document.getElementById('map-wires');
  const edges = [...document.querySelectorAll('[data-edge]')];
  let active = 0;

  function drawConnections() {
    if (!wires) return;
    const bounds = map.getBoundingClientRect();
    wires.setAttribute('viewBox', `0 0 ${bounds.width} ${bounds.height}`);
    const rectangles = nodes.map(node => node.getBoundingClientRect());
    for (const edge of edges) {
      const from = Number(edge.dataset.from), to = Number(edge.dataset.to);
      const source = rectangles[from], target = rectangles[to];
      const others = rectangles.filter((_, n) => n !== from && n !== to);
      let x1, y1, x2, y2, route;
      if (Math.abs(source.top - target.top) < 5) {
        const forward = target.left > source.left;
        x1 = (forward ? source.right : source.left) - bounds.left;
        x2 = (forward ? target.left : target.right) - bounds.left;
        y1 = source.top + source.height / 2 - bounds.top;
        y2 = target.top + target.height / 2 - bounds.top;
        const blocked = others.some(rect => rect.left < Math.max(x1, x2) + bounds.left &&
          rect.right > Math.min(x1, x2) + bounds.left && rect.top < y1 + bounds.top &&
          rect.bottom > y1 + bounds.top);
        if (!blocked) route = `L ${x2} ${y2}`;
        else {
          x1 = source.left + source.width / 2 - bounds.left;
          x2 = target.left + target.width / 2 - bounds.left;
          y1 = (forward ? source.top : source.bottom) - bounds.top;
          y2 = (forward ? target.top : target.bottom) - bounds.top;
          const lane = y1 + (forward ? -12 : 12);
          route = `V ${lane} H ${x2} V ${y2}`;
        }
      } else {
        const forward = target.top > source.top;
        x1 = source.left + source.width / 2 - bounds.left;
        x2 = target.left + target.width / 2 - bounds.left;
        y1 = (forward ? source.bottom : source.top) - bounds.top;
        y2 = (forward ? target.top : target.bottom) - bounds.top;
        const blocked = others.some(rect => rect.top < Math.max(y1, y2) + bounds.top &&
          rect.bottom > Math.min(y1, y2) + bounds.top && rect.left < x1 + bounds.left &&
          rect.right > x1 + bounds.left);
        if (Math.abs(x1 - x2) < 1 && !blocked) route = `L ${x2} ${y2}`;
        else {
          // Row gaps and outside gutters keep skip/reverse edges out of unrelated cards.
          const lane = forward ? 8 : bounds.width - 8;
          const offset = forward ? 12 : -12;
          route = `V ${y1 + offset} H ${lane} V ${y2 - offset} H ${x2} V ${y2}`;
        }
      }
      edge.setAttribute('d', `M ${x1} ${y1} ${route}`);
      edge.classList.toggle('active', Number(edge.dataset.from) === active || Number(edge.dataset.to) === active);
    }
  }

  function select(index, updateHash = true) {
    if (!Number.isInteger(index) || index < 0 || index >= panels.length) return;
    active = index;
    nodes.forEach((node, n) => {
      node.setAttribute('aria-selected', String(n === active));
      node.tabIndex = n === active ? 0 : -1;
      panels[n].hidden = n !== active;
    });
    if (updateHash) history.replaceState(null, '', `#change-${active}`);
    drawConnections();
  }

  document.getElementById('map-nodes').setAttribute('role', 'tablist');
  nodes.forEach((node, index) => {
    node.setAttribute('role', 'tab');
    node.setAttribute('aria-controls', panels[index].id);
    panels[index].setAttribute('role', 'tabpanel');
    panels[index].setAttribute('aria-labelledby', node.id);
    panels[index].tabIndex = 0;
    node.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % nodes.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index + nodes.length - 1) % nodes.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = nodes.length - 1;
      else if (event.key === ' ') next = index;
      else return;
      event.preventDefault();
      select(next);
      nodes[next].focus();
    });
  });
  document.querySelectorAll('[data-select]').forEach(link => {
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const index = Number(link.dataset.select);
      select(index);
      if (!link.hasAttribute('data-map-node')) nodes[index].focus();
      if (window.matchMedia('(max-width: 620px)').matches) panels[index].scrollIntoView({ block: 'nearest' });
    });
  });

  function selectHash() {
    const match = /^#change-(\d+)$/.exec(location.hash);
    const index = match ? Number(match[1]) : active;
    select(index < panels.length ? index : active, false);
  }
  window.addEventListener('hashchange', selectHash);
  window.addEventListener('resize', drawConnections);
  if (window.ResizeObserver) new ResizeObserver(drawConnections).observe(map);
  selectHash();
})();
