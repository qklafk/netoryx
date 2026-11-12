// Monochrome network animation for the hero: drifting nodes, thin links, small travelling packets.
// Pauses when the tab is hidden or the canvas is off-screen; draws a single frame when motion is reduced.

interface Node {
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;
}
interface Packet {
  a: number;
  b: number;
  t: number;
  speed: number;
}

export function initNetwork(canvas: HTMLCanvasElement): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  let w = 0;
  let h = 0;
  let dpr = 1;
  let nodes: Node[] = [];
  let packets: Packet[] = [];
  let links: [number, number][] = [];
  let raf = 0;
  let visible = true;
  let last = 0;
  let rgb = '255 255 255';
  let frame = 0;

  const rand = (a: number, b: number) => a + Math.random() * (b - a);

  function build() {
    const rect = canvas.getBoundingClientRect();
    w = rect.width;
    h = rect.height;
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.max(18, Math.min(60, Math.round(w / 22)));
    nodes = Array.from({ length: count }, () => ({
      x: rand(0, w),
      y: rand(0, h),
      vx: rand(-0.12, 0.12),
      vy: rand(-0.08, 0.08),
      r: Math.random() < 0.15 ? 3 : 1.6,
    }));
    packets = [];
  }

  function relink() {
    const maxD = Math.min(190, Math.max(110, w / 7));
    links = [];
    const degree = new Array(nodes.length).fill(0);
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        if (degree[i] >= 3 || degree[j] >= 3) continue;
        const dx = nodes[i].x - nodes[j].x;
        const dy = nodes[i].y - nodes[j].y;
        if (dx * dx + dy * dy < maxD * maxD) {
          links.push([i, j]);
          degree[i]++;
          degree[j]++;
        }
      }
    }
  }

  function step(dt: number) {
    for (const n of nodes) {
      n.x += n.vx * dt;
      n.y += n.vy * dt;
      if (n.x < -20) n.x = w + 20;
      if (n.x > w + 20) n.x = -20;
      if (n.y < -20) n.y = h + 20;
      if (n.y > h + 20) n.y = -20;
    }
    for (const p of packets) p.t += p.speed * dt;
    packets = packets.filter((p) => p.t < 1);
    const wanted = Math.round(nodes.length / 5);
    if (packets.length < wanted && links.length && Math.random() < 0.08) {
      const [a, b] = links[Math.floor(Math.random() * links.length)];
      packets.push(Math.random() < 0.5 ? { a, b, t: 0, speed: rand(0.004, 0.009) } : { a: b, b: a, t: 0, speed: rand(0.004, 0.009) });
    }
  }

  function draw() {
    ctx!.clearRect(0, 0, w, h);
    ctx!.lineWidth = 1;
    for (const [i, j] of links) {
      const a = nodes[i];
      const b = nodes[j];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const alpha = Math.max(0, 0.38 - d / 600);
      ctx!.strokeStyle = `rgb(${rgb} / ${alpha})`;
      ctx!.beginPath();
      ctx!.moveTo(a.x, a.y);
      ctx!.lineTo(b.x, b.y);
      ctx!.stroke();
    }
    for (const n of nodes) {
      ctx!.fillStyle = `rgb(${rgb} / ${n.r > 2 ? 0.9 : 0.55})`;
      ctx!.fillRect(Math.round(n.x - n.r), Math.round(n.y - n.r), n.r * 2, n.r * 2);
    }
    ctx!.fillStyle = `rgb(${rgb} / 1)`;
    for (const p of packets) {
      const a = nodes[p.a];
      const b = nodes[p.b];
      ctx!.fillRect(Math.round(a.x + (b.x - a.x) * p.t) - 2, Math.round(a.y + (b.y - a.y) * p.t) - 2, 4, 4);
    }
  }

  function tick(ts: number) {
    raf = 0;
    if (!visible || document.hidden || reduced.matches) return;
    if (ts - last >= 33) {
      const dt = Math.min(3, (ts - last) / 16.7);
      last = ts;
      step(dt);
      if (frame++ % 20 === 0) {
        relink();
        rgb = getComputedStyle(document.documentElement).getPropertyValue('--nx-rgb').trim() || rgb;
      }
      draw();
    }
    raf = requestAnimationFrame(tick);
  }

  function start() {
    if (!raf && visible && !document.hidden && !reduced.matches) raf = requestAnimationFrame(tick);
  }

  function renderStatic() {
    rgb = getComputedStyle(document.documentElement).getPropertyValue('--nx-rgb').trim() || rgb;
    for (let i = 0; i < 40; i++) step(1);
    relink();
    for (let i = 0; i < 8 && links.length; i++) {
      const [a, b] = links[Math.floor(Math.random() * links.length)];
      packets.push({ a, b, t: Math.random(), speed: 0 });
    }
    draw();
  }

  function setup() {
    build();
    relink();
    if (reduced.matches) renderStatic();
    else start();
  }

  setup();
  let resizeTimer = 0;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(setup, 200);
  });
  new IntersectionObserver((entries) => {
    visible = entries[0]?.isIntersecting ?? true;
    if (visible) start();
  }).observe(canvas);
  document.addEventListener('visibilitychange', start);
  reduced.addEventListener('change', setup);
  new MutationObserver(() => {
    if (reduced.matches) {
      rgb = getComputedStyle(document.documentElement).getPropertyValue('--nx-rgb').trim() || rgb;
      draw();
    }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
}
