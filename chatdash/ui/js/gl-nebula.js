// WebGL2 volumetric nebula renderer (no libraries). Draws what it is given and nothing else: the caller owns the data
// (field.js maps seats and chats to clusters and lights) and the frame loop (pause rules live there).
//
// Pipeline, all in half-float HDR:
//   1. gas     half-res: per seat cluster, a short front-to-back raymarch through domain-warped 3D fbm inside an
//              ellipsoid (emission + absorption); chat lights illuminate the gas around them; light cores; the
//              needs-you shockwave; an unknown seat is a dark void with a faint scan grid and dashed rim, no gas.
//   2. dust    GL_POINTS riding an analytic curl-noise flow (divergence-free, stateless) around each cluster.
//   3. bloom   dual-filter (Kawase-style) down and up chain over a soft-threshold prefilter.
//   4. present Khronos PBR Neutral tonemap (keeps the seat hues), slight chromatic fringe, blue-noise-ish dither,
//              film grain, soft top and bottom fade; premultiplied alpha over the page's CSS void.
// render(scene, tMs) where scene = { clusters: [{x, y, R, left, kind, seed, lights: [{x, y, needs, idle}]}],
//   hue, out, unk, light, need: [r, g, b] linear-ish 0..1, dark: bool, dust: count }. Coordinates are canvas CSS px.

const MAXC = 8, MAXL = 64;

const VS_QUAD = `#version 300 es
in vec2 aP; out vec2 vUv;
void main() { vUv = aP * 0.5 + 0.5; gl_Position = vec4(aP, 0.0, 1.0); }`;

const NOISE = `
float h13(vec3 p) { p = fract(p * 0.1031); p += dot(p, p.zyx + 31.32); return fract((p.x + p.y) * p.z); }
float h12(vec2 p) { vec3 q = fract(vec3(p.xyx) * 0.1031); q += dot(q, q.yzx + 33.33); return fract((q.x + q.y) * q.z); }
float vnoise(vec3 x) {
  vec3 i = floor(x), f = fract(x); f = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(h13(i), h13(i + vec3(1,0,0)), f.x), mix(h13(i + vec3(0,1,0)), h13(i + vec3(1,1,0)), f.x), f.y),
             mix(mix(h13(i + vec3(0,0,1)), h13(i + vec3(1,0,1)), f.x), mix(h13(i + vec3(0,1,1)), h13(i + vec3(1,1,1)), f.x), f.y), f.z);
}
const mat3 ROT = mat3(0.00, 0.80, 0.60, -0.80, 0.36, -0.48, -0.60, -0.48, 0.64);   // octaves rotated: no grid artefacts
float fbm3(vec3 p) { float a = 0.5, s = 0.0; for (int i = 0; i < 5; i++) { s += a * vnoise(p); p = ROT * p * 2.03 + vec3(1.7, 9.2, 3.1); a *= 0.5; } return s; }
float fbm2(vec2 p) { return fbm3(vec3(p, 0.37)); }
`;

const FS_GAS = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o;
uniform vec2 uSize;          // canvas CSS px
uniform float uT;
uniform int uN, uNL;
uniform vec4 uC[${MAXC}];    // x, y, R, left (0..1, -1 unknown)
uniform vec4 uK[${MAXC}];    // kind (0 ok, 1 out, 2 unknown), first light, light count, seed
uniform vec4 uL[${MAXL}];    // x, y, intensity, needs (0/1)
uniform vec3 uHue, uHue2, uOut, uOut2, uUnk, uLight, uNeed;
uniform float uRing;         // shockwave and core size scale (phone 0.55)
uniform float uF[${MAXC}];   // per-cluster depth fade (1 near, lower far); the board passes 1
uniform vec2 uStar;          // background star parallax offset in CSS px (Sky's camera)
uniform float uDay;          // 1 in the daylight theme: finer light cores, so they read as ink points, not discs
${NOISE}
void main() {
  vec2 P = vUv * uSize; P.y = uSize.y - P.y;           // CSS px, y down like the DOM
  vec3 col = vec3(0.0); float trans = 1.0;
  // faint background stars (depth only; no data), twinkling slowly
  vec2 cell = floor((P + uStar) / 3.0); float hs = h12(cell);
  if (hs > 0.9965) col += vec3(0.75, 0.82, 1.0) * (0.35 + 0.65 * sin(uT * 0.0007 + hs * 70.0) * 0.5 + 0.2) * 0.6;
  for (int i = 0; i < ${MAXC}; i++) {
    if (i >= uN) break;
    vec4 c = uC[i]; vec4 k = uK[i];
    vec2 d = P - c.xy; float R = c.z;
    vec2 q = d / vec2(R * 1.5, R * 0.95);
    float r2 = dot(q, q);
    if (r2 > 2.3) continue;
    if (k.x > 1.5) {                                      // unknown: a dark void, scan grid, dashed rim. Never sized by data.
      q = d / vec2(R * 1.3, R * 0.82); r2 = dot(q, q);
      float inside = 1.0 - smoothstep(0.92, 1.0, r2);
      trans *= 1.0 - 0.55 * inside;
      float grid = smoothstep(0.82, 1.0, fract(P.y / 4.0)) * 0.5 + smoothstep(0.93, 1.0, fract((P.x + P.y) / 9.0)) * 0.35;
      col += uUnk * grid * inside * 0.16;
      float ang = atan(q.y, q.x);
      float rim = (1.0 - smoothstep(0.0, 0.06, abs(sqrt(r2) - 0.97))) * step(0.5, fract(ang * 6.0));
      col += uUnk * rim * 0.9;
      continue;
    }
    float left = c.w;
    vec3 hue = k.x > 0.5 ? uOut : uHue, hue2 = k.x > 0.5 ? uOut2 : uHue2;
    // illumination from this cluster's chat lights, computed once per pixel (2D falloff)
    vec3 lit = vec3(0.0);
    int l0 = int(k.y), ln = int(k.z);
    for (int j = 0; j < 16; j++) {
      if (j >= ln) break;
      vec4 L = uL[l0 + j];
      vec2 dl = (P - L.xy) / (R * 0.55);
      float f = L.z / (1.0 + dot(dl, dl) * 2.2);
      lit += (L.w > 0.5 ? uNeed : uLight) * f;
    }
    vec2 warp = vec2(fbm2(q * 1.6 + vec2(k.w, uT * 0.00004)), fbm2(q * 1.6 + vec2(5.2 - uT * 0.00003, k.w))) * 1.6;
    float edge = (fbm2(q * 1.7 + warp * 0.7 + k.w) - 0.5) * 1.1;   // ragged, never a clean ellipse
    float vigour = (k.x > 0.5 ? 0.6 : 0.25 + 0.75 * left) * uF[i];      // nearly out: a small, still-visible ember
    const int S = 24;
    float dz = 2.0 / float(S);
    float jitter = h12(P + fract(uT * 0.001) * 61.0);
    for (int s = 0; s < S; s++) {
      float z = -1.0 + (float(s) + jitter) * dz;
      float rr = length(vec3(q, z * 1.3));
      float shape = 1.0 - rr + edge;
      if (shape <= 0.0) continue;
      vec3 sp = vec3(q * 2.3 + warp, z * 1.4 + k.w) + vec3(0.0, 0.0, uT * 0.000025);
      float n = fbm3(sp);
      float ridge = 1.0 - abs(n * 2.0 - 1.0); ridge *= ridge; ridge *= ridge;           // thin filaments
      float lane = smoothstep(0.52, 0.7, fbm3(sp * 2.2 + 4.1));                        // dark dust lanes
      float dens = smoothstep(0.0, 0.5, shape) * (smoothstep(0.5, 0.9, n) * 0.7 + ridge * 0.9) * (k.x > 0.5 ? 0.7 : 0.35 + 1.0 * left);
      if (dens < 0.004) continue;
      // lit from a star at the seat's heart (brighter with more week left) and from its chat lights
      float star = vigour * 1.5 / (1.0 + rr * rr * 9.0);
      float a = 1.0 - exp(-dens * dz * 2.6);
      vec3 ramp = mix(hue2 * 0.35, hue, smoothstep(0.15, 0.6, dens));
      ramp = mix(ramp, mix(hue, vec3(1.0, 0.97, 0.92), 0.7), smoothstep(0.75, 1.6, dens * star));
      float heart = exp(-rr * rr * 16.0) * vigour * 2.2;                                // the hot core
      vec3 e = ramp * (0.1 + star) * (1.0 - 0.85 * lane) + vec3(1.0, 0.95, 0.88) * heart * (0.4 + n)
             + lit * (0.25 + dens) * (1.0 - 0.6 * lane);
      col += trans * a * e;
      trans *= 1.0 - a * (0.45 + 0.5 * lane);
      if (trans < 0.02) break;
    }
  }
  // light cores and the needs-you shockwave (HDR > 1 so bloom picks them up)
  for (int j = 0; j < ${MAXL}; j++) {
    if (j >= uNL) break;
    vec4 L = uL[j];
    vec2 dl = P - L.xy; float d2 = dot(dl, dl);
    float need = L.w;
    float core = exp(-d2 / (mix(3.0, 7.0, need) * uRing * uRing * mix(1.0, 0.45, uDay))) * L.z * mix(2.0, 4.0, need) * mix(1.0, mix(0.6, 1.6, need), uDay);
    float halo = exp(-d2 / mix(60.0, 170.0, need)) * L.z * 0.45 * mix(1.0, 0.25, uDay);
    col += (need > 0.5 ? uNeed : uLight) * (core + halo);
    if (need > 0.5) {
      float ph = fract(uT / 2600.0 + h12(L.xy) * 0.37);
      float rad = (6.0 + ph * 46.0) * uRing;
      float ring = exp(-pow((sqrt(d2) - rad) / 1.6, 2.0)) * (1.0 - ph) * (1.0 - ph) * mix(1.4, 3.2, uDay);
      col += uNeed * ring;
    }
  }
  o = vec4(col, 1.0 - trans);
}`;

const VS_DUST = `#version 300 es
in vec4 aS;                   // cluster index, radius frac, angle, speed
uniform vec4 uC[${MAXC}];
uniform vec4 uK[${MAXC}];
uniform float uT; uniform vec2 uSize; uniform float uDay;
out float vA; out float vHot;
vec2 curl(vec2 p, float t) {  // curl of psi = sin(x f + t) cos(y f - 0.7 t): divergence-free flow
  float f = 0.035;
  float dx = cos(p.x * f + t) * f * cos(p.y * f - 0.7 * t);
  float dy = -sin(p.x * f + t) * sin(p.y * f - 0.7 * t) * f;
  return vec2(dy, -dx) * 900.0;
}
void main() {
  int i = int(aS.x);
  vec4 c = uC[i];
  float a = aS.z + uT * aS.w;
  vec2 p = c.xy + vec2(cos(a), sin(a) * 0.62) * aS.y * c.z * 1.55;
  p += curl(p, uT * 0.00011 + aS.z) * 0.012 * c.z / 60.0;
  vA = mix(1.0, 0.3, uDay) * (uK[i].x > 1.5 ? 0.0 : 1.0) * (0.15 + 0.85 * pow(fract(aS.z * 13.7), 4.0)) * smoothstep(1.4, 0.3, aS.y) * 0.7;
  vHot = uK[i].x;
  gl_Position = vec4(p.x / uSize.x * 2.0 - 1.0, 1.0 - p.y / uSize.y * 2.0, 0.0, 1.0);
  gl_PointSize = 1.0 + 1.2 * pow(fract(aS.z * 7.3), 3.0);
}`;

const FS_DUST = `#version 300 es
precision highp float;
in float vA; in float vHot; out vec4 o;
uniform vec3 uHue, uOut;
void main() {
  vec2 d = gl_PointCoord - 0.5; float f = exp(-dot(d, d) * 9.0);
  o = vec4((vHot > 0.5 ? uOut : uHue) * 1.6 * f * vA, 0.0);
}`;

const FS_PRE = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o; uniform sampler2D uTex; uniform vec2 uTexel;
void main() {
  vec3 c = texture(uTex, vUv).rgb * 4.0;
  c += texture(uTex, vUv + uTexel * vec2(-1, -1)).rgb + texture(uTex, vUv + uTexel * vec2(1, -1)).rgb
     + texture(uTex, vUv + uTexel * vec2(-1, 1)).rgb + texture(uTex, vUv + uTexel * vec2(1, 1)).rgb;
  c /= 8.0;
  float b = max(c.r, max(c.g, c.b)); float knee = 0.35, th = 0.55;
  float s = clamp(b - th + knee, 0.0, 2.0 * knee); s = s * s / (4.0 * knee + 1e-4);
  o = vec4(c * max(s, b - th) / max(b, 1e-4), 1.0);
}`;

const FS_DOWN = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o; uniform sampler2D uTex; uniform vec2 uTexel;
void main() {
  vec3 c = texture(uTex, vUv).rgb * 4.0;
  c += texture(uTex, vUv - uTexel).rgb + texture(uTex, vUv + uTexel).rgb
     + texture(uTex, vUv + vec2(uTexel.x, -uTexel.y)).rgb + texture(uTex, vUv - vec2(uTexel.x, -uTexel.y)).rgb;
  o = vec4(c / 8.0, 1.0);
}`;

const FS_UP = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o; uniform sampler2D uTex; uniform vec2 uTexel;
void main() {
  vec2 h = uTexel;
  vec3 c = texture(uTex, vUv + vec2(-h.x * 2.0, 0.0)).rgb + texture(uTex, vUv + vec2(h.x * 2.0, 0.0)).rgb
         + texture(uTex, vUv + vec2(0.0, -h.y * 2.0)).rgb + texture(uTex, vUv + vec2(0.0, h.y * 2.0)).rgb;
  c += (texture(uTex, vUv + vec2(-h.x, h.y)).rgb + texture(uTex, vUv + vec2(h.x, h.y)).rgb
      + texture(uTex, vUv + vec2(-h.x, -h.y)).rgb + texture(uTex, vUv + vec2(h.x, -h.y)).rgb) * 2.0;
  o = vec4(c / 12.0, 1.0);
}`;

const FS_PRESENT = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o;
uniform sampler2D uGas, uBloom; uniform float uT, uDark, uBloomK, uZone, uCssH, uAurora; uniform vec2 uPx; uniform vec3 uInk, uHue, uHue2, uOut;
vec3 neutral(vec3 c) {           // Khronos PBR Neutral: compresses highlights without shifting hue
  const float start = 0.76, desat = 0.15;
  float x = min(c.r, min(c.g, c.b)); float off = x < 0.08 ? x - 6.25 * x * x : 0.04; c -= off;
  float peak = max(c.r, max(c.g, c.b)); if (peak < start) return c;
  float d = 1.0 - start; float np = 1.0 - d * d / (peak + d - start); c *= np / peak;
  float g = 1.0 - 1.0 / (desat * (peak - np) + 1.0); return mix(c, vec3(np), g);
}
float ign(vec2 p) { return fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715)))); }
void main() {
  vec2 rad = vUv - 0.5; vec2 off = rad * dot(rad, rad) * uPx * 2.0;      // fringe only toward the far edges
  vec4 g = texture(uGas, vUv);
  vec3 c = vec3(texture(uGas, vUv + off).r, g.g, texture(uGas, vUv - off).b);
  c += texture(uBloom, vUv).rgb * uBloomK;
  c = neutral(c * 0.95);
  vec2 fc = gl_FragCoord.xy;
  float grain = (ign(fc + fract(uT * 0.0137) * 97.0) - 0.5) / 255.0 * 3.0;
  c += grain;
  float yb = vUv.y * uCssH;                      // CSS px above the canvas bottom
  // a clear floor of uZone px at the bottom (the seat callouts live there, on the bare void), then a soft rise
  float fade = smoothstep(uZone, uZone + 34.0, yb) * smoothstep(1.0, 0.9, vUv.y);
  float a = clamp(max(max(c.r, c.g), c.b) * 1.1 + g.a * 0.55, 0.0, 1.0) * fade;
  if (uDark > 0.5) { o = vec4(clamp(c, 0.0, 1.0) * fade, a); return; }
  if (uAurora > 0.5) {
    // aurora daylight: glows by colour, not brightness. Thin gas is a luminous pastel veil drifting between the seat's
    // two hues with a slow iridescent shimmer; dense cores deepen in chroma (never grey); needs-you stays a deep ink star.
    float L = dot(c, vec3(0.3, 0.55, 0.15));
    float av = (1.0 - exp(-L * 1.9)) * fade;
    float mx = max(max(c.r, max(c.g, c.b)), 1e-3);
    vec3 tint = c / mx;
    float chroma = 1.0 - min(tint.r, min(tint.g, tint.b));          // a seat's own colour (out, unknown) wins when it is vivid
    float sh = 0.5 + 0.5 * sin(dot(fc, vec2(0.0045, 0.0032)) + uT * 0.00018);
    vec3 veil = mix(mix(uHue, uHue2, sh), tint, smoothstep(0.25, 0.7, chroma));
    vec3 vivid = veil * veil; vivid /= max(max(vivid.r, max(vivid.g, vivid.b)), 1e-3);   // squared: more chroma, same hue
    vec3 pastel = mix(vivid, vec3(1.0), 0.55);
    vec3 deep = vivid * 0.74;
    float whiteHot = smoothstep(1.2, 2.4, L);            // light cores and needs-you: ink points, not white blobs
    vec3 col = mix(mix(pastel, deep, smoothstep(0.35, 0.95, av)), uInk, whiteHot * 0.85);
    float al = clamp(av * 0.9 + whiteHot * 0.6, 0.0, 1.0);
    o = vec4(col * al, al); return;
  }
  // daylight, "ink in water": the same emission read as pigment absorbed by paper. Thin gas is a pale wash of the
  // seat's hue, dense gas and lights deepen to indigo ink (never black); over the page's paper via premultiplied alpha.
  float L = dot(c, vec3(0.3, 0.55, 0.15));
  float ink = (1.0 - exp(-L * 2.1)) * fade;
  vec3 tint = c / max(max(c.r, max(c.g, c.b)), 1e-3);
  vec3 wash = mix(mix(tint, uInk, 0.2) * 0.8, uInk, smoothstep(0.35, 0.95, ink));
  o = vec4(wash * ink, ink);
}`;

function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error("shader: " + gl.getShaderInfoLog(s));
  return s;
}
function program(gl, vs, fs) {
  const p = gl.createProgram();
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs)); gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error("link: " + gl.getProgramInfoLog(p));
  const u = new Proxy({}, { get: (c, k) => (k in c ? c[k] : (c[k] = gl.getUniformLocation(p, k))) });
  return { p, u };
}

export function glSupported() {
  try { const c = document.createElement("canvas"); return !!(c.getContext("webgl2") && c.getContext("webgl2").getExtension("EXT_color_buffer_half_float") !== undefined); }
  catch { return false; }
}

export class GLNebula {
  constructor(canvas) {
    const gl = canvas.getContext("webgl2", { premultipliedAlpha: true, alpha: true, antialias: false, powerPreference: "high-performance" });
    if (!gl) throw new Error("no webgl2");
    this.gl = gl; this.canvas = canvas;
    this.float = !!(gl.getExtension("EXT_color_buffer_float") || gl.getExtension("EXT_color_buffer_half_float"));
    this.P = {
      gas: program(gl, VS_QUAD, FS_GAS), dust: program(gl, VS_DUST, FS_DUST), pre: program(gl, VS_QUAD, FS_PRE),
      down: program(gl, VS_QUAD, FS_DOWN), up: program(gl, VS_QUAD, FS_UP), present: program(gl, VS_QUAD, FS_PRESENT),
    };
    this.quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    this.dustBuf = gl.createBuffer(); this.dustN = 0; this.dustKey = "";
    this.cArr = new Float32Array(MAXC * 4); this.kArr = new Float32Array(MAXC * 4); this.lArr = new Float32Array(MAXL * 4);
    this.targets = null;
    this.lost = false;
    canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); this.lost = true; });
  }

  resize(cssW, cssH, dpr) {
    const gl = this.gl, W = Math.max(1, Math.round(cssW * dpr)), H = Math.max(1, Math.round(cssH * dpr));
    this.css = [cssW, cssH];
    if (this.canvas.width === W && this.canvas.height === H && this.targets) return;
    this.canvas.width = W; this.canvas.height = H;
    if (this.targets) this.targets.forEach((t) => { gl.deleteTexture(t.tex); gl.deleteFramebuffer(t.fb); });
    const mk = (w, h) => {
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, this.float ? gl.RGBA16F : gl.RGBA8, w, h, 0, gl.RGBA, this.float ? gl.HALF_FLOAT : gl.UNSIGNED_BYTE, null);
      for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
      const fb = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      return { tex, fb, w, h };
    };
    // gas at half the CSS size (dpr-independent cost), bloom chain below it
    const k = this.gasScale || 0.75;
    const gw = Math.max(2, Math.round(cssW * k)), gh = Math.max(2, Math.round(cssH * k));
    this.targets = [mk(gw, gh)];
    let w = gw, h = gh;
    for (let i = 0; i < 5; i++) { w = Math.max(1, w >> 1); h = Math.max(1, h >> 1); this.targets.push(mk(w, h)); }
  }

  dust(scene) {
    const n = scene.dust || 0, key = n + "|" + scene.clusters.map((c) => c.seed + ":" + Math.round((c.left ?? 0) * 20)).join(",");
    if (key === this.dustKey) return;
    this.dustKey = key;
    const shares = scene.clusters.map((c) => (c.kind === "unknown" ? 0 : 0.15 + (c.left || 0)));
    const tot = shares.reduce((a, b) => a + b, 0) || 1;
    const a = [];
    let s = 1;
    const r = () => ((s = (s * 16807) % 2147483647) / 2147483647);
    scene.clusters.forEach((c, i) => {
      const m = Math.round((n * shares[i]) / tot);
      for (let j = 0; j < m; j++) a.push(i, Math.pow(r(), 0.7) * 1.15, r() * 6.2832, (0.00003 + r() * 0.00009) * (r() < 0.5 ? -1 : 1));
    });
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.dustBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(a), gl.STATIC_DRAW);
    this.dustN = a.length / 4;
  }

  quadDraw(prog) {
    const gl = this.gl, loc = gl.getAttribLocation(prog.p, "aP");
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.disableVertexAttribArray(loc);
  }

  render(scene, t) {
    if (this.lost || !this.targets) return false;
    const gl = this.gl, P = this.P, [cw, ch] = this.css, T = this.targets;
    const cs = scene.clusters.slice(0, MAXC);
    let nl = 0;
    this.cArr.fill(0); this.kArr.fill(0); this.lArr.fill(0);
    cs.forEach((c, i) => {
      this.cArr.set([c.x, c.y, c.R, c.kind === "unknown" ? -1 : c.left], i * 4);
      const first = nl;
      for (const l of c.lights || []) { if (nl >= MAXL) break; this.lArr.set([l.x, l.y, l.needs ? 1.25 : l.idle ? 0.35 : 0.8, l.needs ? 1 : 0], nl * 4); nl++; }
      this.kArr.set([c.kind === "out" ? 1 : c.kind === "unknown" ? 2 : 0, first, nl - first, c.seed], i * 4);
    });
    this.dust(scene);
    // 1. gas
    gl.bindFramebuffer(gl.FRAMEBUFFER, T[0].fb); gl.viewport(0, 0, T[0].w, T[0].h);
    gl.disable(gl.BLEND); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(P.gas.p);
    const U = P.gas.u;
    gl.uniform2f(U.uSize, cw, ch); gl.uniform1f(U.uT, t); gl.uniform1i(U.uN, cs.length); gl.uniform1i(U.uNL, nl);
    gl.uniform4fv(U.uC, this.cArr); gl.uniform4fv(U.uK, this.kArr); gl.uniform4fv(U.uL, this.lArr);
    gl.uniform3fv(U.uHue, scene.hue); gl.uniform3fv(U.uOut, scene.out); gl.uniform3fv(U.uUnk, scene.unk);
    gl.uniform1f(U.uRing, scene.ring || 1);
    const fades = new Float32Array(MAXC).fill(1);
    cs.forEach((c, i) => { if (c.fade != null) fades[i] = c.fade; });
    gl.uniform1fv(U.uF, fades); gl.uniform1f(U.uDay, scene.dark ? 0 : 1); gl.uniform2fv(U.uStar, scene.star || [0, 0]);
    gl.uniform3fv(U.uHue2, scene.hue2 || scene.hue); gl.uniform3fv(U.uOut2, scene.out2 || scene.out);
    gl.uniform3fv(U.uLight, scene.light); gl.uniform3fv(U.uNeed, scene.need);
    this.quadDraw(P.gas);
    // 2. dust, additive into the gas target
    if (this.dustN) {
      gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
      gl.useProgram(P.dust.p);
      const D = P.dust.u;
      gl.uniform4fv(D.uC, this.cArr); gl.uniform4fv(D.uK, this.kArr); gl.uniform1f(D.uT, t); gl.uniform2f(D.uSize, cw, ch); gl.uniform1f(D.uDay, scene.dark ? 0 : 1);
      gl.uniform3fv(D.uHue, scene.hue); gl.uniform3fv(D.uOut, scene.out);
      const loc = gl.getAttribLocation(P.dust.p, "aS");
      gl.bindBuffer(gl.ARRAY_BUFFER, this.dustBuf);
      gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 4, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.POINTS, 0, this.dustN);
      gl.disableVertexAttribArray(loc);
      gl.disable(gl.BLEND);
    }
    // 3. bloom: prefilter into T1, down to T5, then up back to T1 (additive)
    const pass = (prog, src, dst) => {
      gl.bindFramebuffer(gl.FRAMEBUFFER, dst.fb); gl.viewport(0, 0, dst.w, dst.h);
      gl.useProgram(prog.p);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, src.tex);
      gl.uniform1i(prog.u.uTex, 0); gl.uniform2f(prog.u.uTexel, 0.5 / src.w, 0.5 / src.h);
      this.quadDraw(prog);
    };
    pass(P.pre, T[0], T[1]);
    for (let i = 1; i < 5; i++) pass(P.down, T[i], T[i + 1]);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
    for (let i = 5; i > 1; i--) pass(P.up, T[i], T[i - 1]);
    gl.disable(gl.BLEND);
    // 4. present
    gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(P.present.p);
    const R = P.present.u;
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, T[0].tex); gl.uniform1i(R.uGas, 0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, T[1].tex); gl.uniform1i(R.uBloom, 1);
    gl.uniform1f(R.uZone, scene.zone || 0); gl.uniform1f(R.uCssH, ch); gl.uniform1f(R.uAurora, scene.mode === "aurora" ? 1 : 0);
    gl.uniform3fv(R.uHue, scene.hue); gl.uniform3fv(R.uHue2, scene.hue2 || scene.hue); gl.uniform3fv(R.uOut, scene.out);
    gl.uniform1f(R.uT, t); gl.uniform1f(R.uDark, scene.dark ? 1 : 0); gl.uniform1f(R.uBloomK, 0.55);
    gl.uniform2f(R.uPx, 1 / T[0].w, 1 / T[0].h); gl.uniform3fv(R.uInk, scene.ink || [0.1, 0.12, 0.25]);
    this.quadDraw(P.present);
    return true;
  }
}
