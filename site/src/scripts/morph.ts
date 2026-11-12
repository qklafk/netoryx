// Dot-matrix wireframe that morphs between four shapes as the active step changes.

type P = [number, number, number];
const N = 520;

const sphere = (n: number, r: number, c: P = [0, 0, 0]): P[] =>
  Array.from({ length: n }, (_, i) => {
    const y = 1 - (i / (n - 1)) * 2;
    const rad = Math.sqrt(1 - y * y);
    const th = i * 2.399963;
    return [c[0] + Math.cos(th) * rad * r, c[1] + y * r, c[2] + Math.sin(th) * rad * r];
  });

function grid(): P[] {
  const side = Math.ceil(Math.sqrt(N));
  const out: P[] = [];
  for (let i = 0; i < N; i++) {
    const gx = i % side;
    const gz = Math.floor(i / side);
    out.push([(gx / (side - 1) - 0.5) * 2.1, 0.55 + Math.sin(gx * 0.5) * 0.04, (gz / (side - 1) - 0.5) * 2.1]);
  }
  return out;
}

function hub(): P[] {
  const out: P[] = [[0, 0, 0]];
  const rings = [0.45, 0.8, 1.15];
  const counts = [60, 110, 160];
  for (let r = 0; r < rings.length; r++) {
    for (let k = 0; k < counts[r] && out.length < N; k++) {
      const a = (k / counts[r]) * Math.PI * 2;
      out.push([Math.cos(a) * rings[r], 0, Math.sin(a) * rings[r]]);
    }
  }
  for (let k = 0; out.length < N; k++) {
    const a = (k % 8) * (Math.PI / 4);
    const t = ((Math.floor(k / 8) + 1) / 22) * 1.15;
    out.push([Math.cos(a) * t, 0, Math.sin(a) * t]);
  }
  return out;
}

function clients(): P[] {
  const centers: P[] = [
    [0, 0, 0],
    [-1.05, 0.35, 0.3],
    [1.0, -0.3, 0.4],
    [-0.4, -0.85, -0.6],
    [0.6, 0.8, -0.5],
    [0.05, 0.95, 0.5],
  ];
  const out: P[] = [];
  centers.forEach((c, i) => {
    out.push(...sphere(i === 0 ? 170 : 70, i === 0 ? 0.5 : 0.24, c));
  });
  return out.slice(0, N);
}

const shapes: P[][] = [sphere(N, 1), grid(), hub(), clients()];

export function initMorph(canvas: HTMLCanvasElement): { set(step: number): void } {
  const ctx = canvas.getContext('2d');
  if (!ctx) return { set() {} };
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const cur: P[] = shapes[0].map((p) => [...p] as P);
  let target = 0;
  let w = 0;
  let h = 0;
  let angle = 0.6;
  let raf = 0;
  let visible = true;
  let rgb = '255 255 255';

  function readColor() {
    rgb = getComputedStyle(document.documentElement).getPropertyValue('--nx-rgb').trim() || rgb;
  }

  function draw() {
    ctx!.clearRect(0, 0, w, h);
    const size = Math.min(w, h) * 0.36;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const tilt = target === 1 ? 0.62 : target === 2 ? 0.9 : 0.25;
    const ct = Math.cos(tilt);
    const st = Math.sin(tilt);
    for (const p of cur) {
      const x = p[0] * cos - p[2] * sin;
      const z0 = p[0] * sin + p[2] * cos;
      const y = p[1] * ct - z0 * st;
      const z = p[1] * st + z0 * ct;
      const persp = 1 / (1 - z * 0.28);
      const sx = w / 2 + x * size * persp;
      const sy = h / 2 + y * size * persp;
      const a = 0.25 + 0.75 * Math.max(0, Math.min(1, (z + 1.3) / 2.6));
      ctx!.fillStyle = `rgb(${rgb} / ${a})`;
      const s = 1.4 + a * 1.2;
      ctx!.fillRect(Math.round(sx), Math.round(sy), s, s);
    }
  }

  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    w = r.width;
    h = r.height;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    readColor();
    draw();
  }

  function tick() {
    raf = 0;
    if (!visible || document.hidden || reduced.matches) return;
    angle += 0.006;
    const goal = shapes[target];
    for (let i = 0; i < cur.length; i++) {
      for (let k = 0; k < 3; k++) cur[i][k] += (goal[i][k] - cur[i][k]) * 0.07;
    }
    draw();
    raf = requestAnimationFrame(tick);
  }

  function start() {
    if (!raf && visible && !document.hidden && !reduced.matches) raf = requestAnimationFrame(tick);
  }

  resize();
  window.addEventListener('resize', resize);
  new IntersectionObserver((e) => {
    visible = e[0]?.isIntersecting ?? true;
    start();
  }).observe(canvas);
  document.addEventListener('visibilitychange', start);
  new MutationObserver(() => {
    readColor();
    draw();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  start();

  return {
    set(step: number) {
      target = Math.max(0, Math.min(shapes.length - 1, step));
      if (reduced.matches) {
        shapes[target].forEach((p, i) => (cur[i] = [...p] as P));
        draw();
      } else start();
    },
  };
}
