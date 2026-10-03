/**
 * LIGHT VS GUTS - Box Arena Battle Simulator
 * Canvas-based physics simulation & interactive playable game.
 */

(function () {
  'use strict';

  // Canvas & Context
  const canvas = document.getElementById('arena-canvas');
  const ctx = canvas.getContext('2d');

  // Virtual Canvas Resolution (faithful 9:16 ratio matching TikTok video)
  const V_WIDTH = 576;
  const V_HEIGHT = 1024;
  canvas.width = V_WIDTH;
  canvas.height = V_HEIGHT;

  // Arena Dimensions inside the 576x1024 canvas
  const ARENA = {
    x: 28,
    y: 280,
    width: 520,
    height: 520,
    borderWidth: 8,
    bgColor: '#646568',
    borderColor: '#000000',
    outerBg: '#81a090',
  };

  // Asset Loader
  const assets = {
    guts: new Image(),
    gutsFlash: new Image(),
    sword: new Image(),
    lightWriting: new Image(),
    lightPointing: new Image(),
    lightFlash: new Image(),
    bgm: new Audio('assets/bgm.mp3'),
  };

  assets.guts.src = 'assets/guts.png';
  assets.gutsFlash.src = 'assets/guts_flash.png';
  assets.sword.src = 'assets/sword.png';
  assets.lightWriting.src = 'assets/light_writing.png';
  assets.lightPointing.src = 'assets/light_pointing.png';
  assets.lightFlash.src = 'assets/light_flash.png';
  assets.bgm.loop = true;

  // Audio Synthesizer (Web Audio API)
  class SoundManager {
    constructor() {
      this.ctx = null;
      this.enabled = true;
      this.bgmEnabled = false;
    }

    init() {
      if (!this.ctx) {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (AudioCtx) this.ctx = new AudioCtx();
      }
      if (this.ctx && this.ctx.state === 'suspended') {
        this.ctx.resume();
      }
    }

    playSwordSwing() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;

      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      const filter = this.ctx.createBiquadFilter();

      osc.type = 'triangle';
      osc.frequency.setValueAtTime(140, this.ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(320, this.ctx.currentTime + 0.08);
      osc.frequency.exponentialRampToValueAtTime(80, this.ctx.currentTime + 0.18);

      filter.type = 'lowpass';
      filter.frequency.setValueAtTime(600, this.ctx.currentTime);

      gain.gain.setValueAtTime(0.15, this.ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, this.ctx.currentTime + 0.18);

      osc.connect(filter);
      filter.connect(gain);
      gain.connect(this.ctx.destination);

      osc.start();
      osc.stop(this.ctx.currentTime + 0.2);
    }

    playSwordHit() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;

      // Heavy metallic clash + noise punch
      const now = this.ctx.currentTime;
      
      // Tone 1
      const osc1 = this.ctx.createOscillator();
      const gain1 = this.ctx.createGain();
      osc1.type = 'sawtooth';
      osc1.frequency.setValueAtTime(580, now);
      osc1.frequency.exponentialRampToValueAtTime(120, now + 0.25);
      gain1.gain.setValueAtTime(0.3, now);
      gain1.gain.exponentialRampToValueAtTime(0.01, now + 0.25);

      osc1.connect(gain1);
      gain1.connect(this.ctx.destination);
      osc1.start(now);
      osc1.stop(now + 0.26);

      // Noise punch
      const bufferSize = this.ctx.sampleRate * 0.1;
      const buffer = this.ctx.createBuffer(1, bufferSize, this.ctx.sampleRate);
      const data = buffer.getChannelData(0);
      for (let i = 0; i < bufferSize; i++) {
        data[i] = (Math.random() * 2 - 1) * Math.exp(-i / (bufferSize * 0.2));
      }
      const noise = this.ctx.createBufferSource();
      noise.buffer = buffer;
      const nGain = this.ctx.createGain();
      nGain.gain.setValueAtTime(0.4, now);
      nGain.gain.exponentialRampToValueAtTime(0.01, now + 0.1);
      noise.connect(nGain);
      nGain.connect(this.ctx.destination);
      noise.start(now);
    }

    playDeathNoteClimax() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;

      const now = this.ctx.currentTime;
      // Deep sub bass boom
      const sub = this.ctx.createOscillator();
      const sGain = this.ctx.createGain();
      sub.type = 'sine';
      sub.frequency.setValueAtTime(90, now);
      sub.frequency.exponentialRampToValueAtTime(30, now + 1.2);
      sGain.gain.setValueAtTime(0.6, now);
      sGain.gain.exponentialRampToValueAtTime(0.01, now + 1.2);
      sub.connect(sGain);
      sGain.connect(this.ctx.destination);
      sub.start(now);
      sub.stop(now + 1.25);

      // Dramatic chord strike
      [440, 554.37, 659.25, 830.61].forEach((freq) => {
        const osc = this.ctx.createOscillator();
        const g = this.ctx.createGain();
        osc.type = 'sawtooth';
        osc.frequency.setValueAtTime(freq, now);
        g.gain.setValueAtTime(0.12, now);
        g.gain.exponentialRampToValueAtTime(0.001, now + 1.5);
        osc.connect(g);
        g.connect(this.ctx.destination);
        osc.start(now);
        osc.stop(now + 1.5);
      });
    }

    playTick() {
      if (!this.enabled) return;
      this.init();
      if (!this.ctx) return;
      const now = this.ctx.currentTime;
      const osc = this.ctx.createOscillator();
      const g = this.ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(900, now);
      g.gain.setValueAtTime(0.08, now);
      g.gain.exponentialRampToValueAtTime(0.001, now + 0.04);
      osc.connect(g);
      g.connect(this.ctx.destination);
      osc.start(now);
      osc.stop(now + 0.05);
    }

    toggleBGM(force) {
      if (force !== undefined) this.bgmEnabled = force;
      else this.bgmEnabled = !this.bgmEnabled;

      if (this.bgmEnabled) {
        assets.bgm.currentTime = 0;
        assets.bgm.play().catch(() => {});
      } else {
        assets.bgm.pause();
      }
      return this.bgmEnabled;
    }
  }

  const sound = new SoundManager();

  // Floating Damage Number System
  class DamageText {
    constructor(x, y, text, color = '#ffd700', isCrit = false) {
      this.x = x;
      this.y = y;
      this.text = text;
      this.color = color;
      this.isCrit = isCrit;
      this.opacity = 1.0;
      this.life = 0;
      this.maxLife = isCrit ? 1.8 : 0.85;
      this.scale = isCrit ? 1.4 : 1.0;
    }

    update(dt) {
      this.life += dt;
      this.y -= 35 * dt;
      if (this.life > this.maxLife * 0.5) {
        this.opacity = Math.max(0, 1.0 - (this.life - this.maxLife * 0.5) / (this.maxLife * 0.5));
      }
      return this.life < this.maxLife;
    }

    draw(ctx) {
      ctx.save();
      ctx.globalAlpha = this.opacity;
      ctx.font = this.isCrit ? '900 42px Montserrat, Orbitron, sans-serif' : '700 34px Montserrat, sans-serif';
      ctx.fillStyle = this.color;
      ctx.strokeStyle = 'rgba(0,0,0,0.85)';
      ctx.lineWidth = this.isCrit ? 6 : 4;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.strokeText(this.text, this.x, this.y);
      ctx.fillText(this.text, this.x, this.y);
      ctx.restore();
    }
  }

  // Particle System (Sparks / Disintegration)
  class Particle {
    constructor(x, y, vx, vy, color, size, life) {
      this.x = x;
      this.y = y;
      this.vx = vx;
      this.vy = vy;
      this.color = color;
      this.size = size;
      this.life = life;
      this.maxLife = life;
    }

    update(dt) {
      this.x += this.vx * dt;
      this.y += this.vy * dt;
      this.vx *= 0.95;
      this.vy *= 0.95;
      this.life -= dt;
      return this.life > 0;
    }

    draw(ctx) {
      ctx.save();
      const alpha = Math.max(0, this.life / this.maxLife);
      ctx.globalAlpha = alpha;
      ctx.fillStyle = this.color;
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.size * alpha, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }

  // Game Engine & State
  class BattleEngine {
    constructor() {
      this.mode = 'cinematic'; // 'cinematic' | 'simulation' | 'play-guts' | 'play-light' | 'sandbox'
      this.running = false;
      this.paused = false;
      this.matchTime = 0;
      this.speedMultiplier = 1.0;
      this.autoRestart = true;
      this.showHitboxes = false;

      // Screen Shake
      this.shakeDuration = 0;
      this.shakeIntensity = 0;

      // Stats
      this.stats = {
        lightWins: 0,
        gutsWins: 0,
        totalMatches: 0,
        hitsLanded: 0,
      };

      // Configuration Parameters (Sandbox Modifiable)
      this.config = {
        deathNoteTime: 15.0,
        swordLengthMult: 1.0,
        swordSpinSpeed: 1.2, // rotations per second
        gutsSpeed: 1.0,
        lightSpeed: 1.0,
        lightMaxHP: 100,
        gutsMaxHP: 100,
      };

      // Entities
      this.light = null;
      this.guts = null;
      this.damageTexts = [];
      this.particles = [];

      // Input State
      this.keys = {};
      this.pointer = { x: 0, y: 0, active: false };
      this.joystick = { dx: 0, dy: 0, active: false };

      this.initInputs();
      this.resetMatch();
    }

    initInputs() {
      window.addEventListener('keydown', (e) => {
        this.keys[e.key.toLowerCase()] = true;
        this.keys[e.code] = true;
        sound.init();
      });

      window.addEventListener('keyup', (e) => {
        this.keys[e.key.toLowerCase()] = false;
        this.keys[e.code] = false;
      });

      // Canvas Pointer Dragging
      const getCanvasPos = (evt) => {
        const rect = canvas.getBoundingClientRect();
        const clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
        const clientY = evt.touches ? evt.touches[0].clientY : evt.clientY;
        const scaleX = V_WIDTH / rect.width;
        const scaleY = V_HEIGHT / rect.height;
        return {
          x: (clientX - rect.left) * scaleX,
          y: (clientY - rect.top) * scaleY,
        };
      };

      canvas.addEventListener('mousedown', (e) => {
        const pos = getCanvasPos(e);
        this.pointer.x = pos.x;
        this.pointer.y = pos.y;
        this.pointer.active = true;
        sound.init();
      });

      window.addEventListener('mousemove', (e) => {
        if (!this.pointer.active) return;
        const pos = getCanvasPos(e);
        this.pointer.x = pos.x;
        this.pointer.y = pos.y;
      });

      window.addEventListener('mouseup', () => {
        this.pointer.active = false;
      });

      canvas.addEventListener('touchstart', (e) => {
        const pos = getCanvasPos(e);
        this.pointer.x = pos.x;
        this.pointer.y = pos.y;
        this.pointer.active = true;
        sound.init();
      }, { passive: false });

      canvas.addEventListener('touchmove', (e) => {
        if (!this.pointer.active) return;
        const pos = getCanvasPos(e);
        this.pointer.x = pos.x;
        this.pointer.y = pos.y;
      }, { passive: false });

      canvas.addEventListener('touchend', () => {
        this.pointer.active = false;
      });
    }

    resetMatch() {
      this.matchTime = 0;
      this.damageTexts = [];
      this.particles = [];
      this.shakeDuration = 0;
      this.winner = null;
      this.lastSecondTick = -1;

      // Base Arena Dimensions
      const arenaLeft = ARENA.x + 40;
      const arenaRight = ARENA.x + ARENA.width - 40;
      const arenaTop = ARENA.y + 50;
      const arenaBottom = ARENA.y + ARENA.height - 50;

      // Setup Light
      this.light = {
        x: ARENA.x + 130,
        y: ARENA.y + 140,
        vx: 45 * this.config.lightSpeed,
        vy: 35 * this.config.lightSpeed,
        radius: 32,
        maxHp: this.config.lightMaxHP,
        hp: this.config.lightMaxHP,
        dnTimer: this.config.deathNoteTime,
        maxDnTimer: this.config.deathNoteTime,
        state: 'writing', // 'writing' | 'pointing' | 'dead'
        flashTimer: 0,
        targetX: ARENA.x + 130,
        targetY: ARENA.y + 140,
      };

      // Setup Guts
      this.guts = {
        x: ARENA.x + ARENA.width - 140,
        y: ARENA.y + ARENA.height - 120,
        vx: -60 * this.config.gutsSpeed,
        vy: -50 * this.config.gutsSpeed,
        radius: 36,
        maxHp: this.config.gutsMaxHP,
        hp: this.config.gutsMaxHP,
        state: 'alive', // 'alive' | 'dead'
        flashTimer: 0,
        swordAngle: 0,
        swordBaseLength: 175,
        hitCooldown: 0,
        targetX: ARENA.x + ARENA.width - 140,
        targetY: ARENA.y + ARENA.height - 120,
      };

      // Setup Cinematic Timeline if in cinematic mode
      if (this.mode === 'cinematic') {
        this.initCinematicScript();
      }

      this.updateHUD();
      this.hideWinnerModal();
    }

    initCinematicScript() {
      // Re-creates the 10 hits across 15.0s leading to Light's survival with 10 HP
      // Hit timestamps (in seconds)
      this.cinematicHits = [
        { time: 1.6, dealt: false },
        { time: 3.2, dealt: false },
        { time: 4.8, dealt: false },
        { time: 6.4, dealt: false },
        { time: 8.0, dealt: false },
        { time: 9.6, dealt: false },
        { time: 11.2, dealt: false },
        { time: 12.8, dealt: false },
        { time: 14.4, dealt: false },
      ];
    }

    shake(intensity = 8, duration = 0.25) {
      this.shakeIntensity = intensity;
      this.shakeDuration = duration;
    }

    spawnHitSparks(x, y, count = 15) {
      for (let i = 0; i < count; i++) {
        const angle = Math.random() * Math.PI * 2;
        const speed = 70 + Math.random() * 160;
        const vx = Math.cos(angle) * speed;
        const vy = Math.sin(angle) * speed;
        const color = Math.random() > 0.4 ? '#ffd700' : '#ffffff';
        this.particles.push(new Particle(x, y, vx, vy, color, 2.5 + Math.random() * 2, 0.25 + Math.random() * 0.25));
      }
    }

    spawnBloodDisintegrate(x, y, count = 50) {
      for (let i = 0; i < count; i++) {
        const angle = Math.random() * Math.PI * 2;
        const speed = 40 + Math.random() * 240;
        const vx = Math.cos(angle) * speed;
        const vy = Math.sin(angle) * speed;
        const color = Math.random() > 0.5 ? '#ff2233' : '#111111';
        this.particles.push(new Particle(x, y, vx, vy, color, 3 + Math.random() * 4, 0.6 + Math.random() * 0.6));
      }
    }

    // Line segment vs Circle collision detection for Dragon Slayer sword
    checkSwordHit(swordStartX, swordStartY, swordEndX, swordEndY, targetX, targetY, targetRadius) {
      const dx = swordEndX - swordStartX;
      const dy = swordEndY - swordStartY;
      const lenSq = dx * dx + dy * dy;

      if (lenSq === 0) {
        const dist = Math.hypot(targetX - swordStartX, targetY - swordStartY);
        return dist <= targetRadius;
      }

      // Project circle center onto segment
      const t = Math.max(0, Math.min(1, ((targetX - swordStartX) * dx + (targetY - swordStartY) * dy) / lenSq));
      const projX = swordStartX + t * dx;
      const projY = swordStartY + t * dy;

      const dist = Math.hypot(targetX - projX, targetY - projY);
      return dist <= targetRadius + 10; // 10px blade half-width
    }

    update(dt) {
      if (!this.running || this.paused || this.winner) return;

      const effectiveDt = dt * this.speedMultiplier;
      this.matchTime += effectiveDt;

      // Screen Shake update
      if (this.shakeDuration > 0) {
        this.shakeDuration -= dt;
      }

      // Update Damage numbers & particles
      this.damageTexts = this.damageTexts.filter((t) => t.update(effectiveDt));
      this.particles = this.particles.filter((p) => p.update(effectiveDt));

      // Audio Ticks per second
      const currentSec = Math.floor(this.light.dnTimer);
      if (currentSec !== this.lastSecondTick && this.light.state === 'writing') {
        this.lastSecondTick = currentSec;
        if (currentSec <= 5 && currentSec >= 1) sound.playTick();
      }

      // 1. Update Light's Death Note Timer
      if (this.light.state === 'writing') {
        this.light.dnTimer = Math.max(0, this.light.dnTimer - effectiveDt);

        // Flash timer decrement
        if (this.light.flashTimer > 0) {
          this.light.flashTimer -= effectiveDt;
        }

        // TRIGGER DEATH NOTE INSTANT KILL!
        if (this.light.dnTimer <= 0) {
          this.triggerLightDeathNote();
          return;
        }
      }

      // 2. Update Guts Sword Rotation
      const spinSpeed = Math.PI * 2 * this.config.swordSpinSpeed;
      this.guts.swordAngle = (this.guts.swordAngle + spinSpeed * effectiveDt) % (Math.PI * 2);

      if (this.guts.hitCooldown > 0) {
        this.guts.hitCooldown -= effectiveDt;
      }
      if (this.guts.flashTimer > 0) {
        this.guts.flashTimer -= effectiveDt;
      }

      // 3. Movement & Behavior Handling by Mode
      this.handleMovement(effectiveDt);

      // 4. Hit Detection (Sword vs Light)
      if (this.light.state === 'writing' && this.guts.state === 'alive') {
        if (this.mode === 'cinematic') {
          this.handleCinematicHits();
        } else {
          this.handlePhysicsSwordCollision();
        }
      }

      // Update HUD timer
      this.updateHUD();
    }

    handleMovement(dt) {
      const arenaMinX = ARENA.x + 36;
      const arenaMaxX = ARENA.x + ARENA.width - 36;
      const arenaMinY = ARENA.y + 40;
      const arenaMaxY = ARENA.y + ARENA.height - 40;

      // Mode: PLAY AS GUTS
      if (this.mode === 'play-guts') {
        let mx = 0;
        let my = 0;
        if (this.keys['w'] || this.keys['arrowup']) my -= 1;
        if (this.keys['s'] || this.keys['arrowdown']) my += 1;
        if (this.keys['a'] || this.keys['arrowleft']) mx -= 1;
        if (this.keys['d'] || this.keys['arrowright']) mx += 1;

        if (this.pointer.active) {
          const dx = this.pointer.x - this.guts.x;
          const dy = this.pointer.y - this.guts.y;
          const dist = Math.hypot(dx, dy);
          if (dist > 10) {
            mx = dx / dist;
            my = dy / dist;
          }
        }

        const moveSpeed = 220 * this.config.gutsSpeed;
        this.guts.x += mx * moveSpeed * dt;
        this.guts.y += my * moveSpeed * dt;

        // Light AI Evasion
        this.runLightEvasionAI(dt);
      }
      // Mode: PLAY AS LIGHT
      else if (this.mode === 'play-light') {
        let mx = 0;
        let my = 0;
        if (this.keys['w'] || this.keys['arrowup']) my -= 1;
        if (this.keys['s'] || this.keys['arrowdown']) my += 1;
        if (this.keys['a'] || this.keys['arrowleft']) mx -= 1;
        if (this.keys['d'] || this.keys['arrowright']) mx += 1;

        if (this.pointer.active) {
          const dx = this.pointer.x - this.light.x;
          const dy = this.pointer.y - this.light.y;
          const dist = Math.hypot(dx, dy);
          if (dist > 10) {
            mx = dx / dist;
            my = dy / dist;
          }
        }

        const moveSpeed = 190 * this.config.lightSpeed;
        this.light.x += mx * moveSpeed * dt;
        this.light.y += my * moveSpeed * dt;

        // Guts AI Chaser
        this.runGutsChaserAI(dt);
      }
      // Mode: CINEMATIC RE-ENACTMENT
      else if (this.mode === 'cinematic') {
        // Smooth cinematic floating/wandering matching the TikTok video
        const t = this.matchTime;
        
        // Light dances around left-upper corner
        this.light.x = ARENA.x + 130 + Math.sin(t * 0.9) * 75 + Math.cos(t * 1.7) * 35;
        this.light.y = ARENA.y + 160 + Math.cos(t * 1.1) * 70 + Math.sin(t * 2.1) * 30;

        // Guts sweeps across arena
        this.guts.x = ARENA.x + ARENA.width - 150 + Math.cos(t * 0.8) * 110 + Math.sin(t * 1.5) * 40;
        this.guts.y = ARENA.y + ARENA.height - 150 + Math.sin(t * 0.7) * 100 + Math.cos(t * 1.8) * 35;
      }
      // Mode: SIMULATION / SANDBOX
      else {
        // Physics Bouncing & AI Steering
        this.runGutsChaserAI(dt);
        this.runLightEvasionAI(dt);
      }

      // Clamp inside arena walls
      this.light.x = Math.max(arenaMinX, Math.min(arenaMaxX, this.light.x));
      this.light.y = Math.max(arenaMinY, Math.min(arenaMaxY, this.light.y));
      this.guts.x = Math.max(arenaMinX, Math.min(arenaMaxX, this.guts.x));
      this.guts.y = Math.max(arenaMinY, Math.min(arenaMaxY, this.guts.y));
    }

    runGutsChaserAI(dt) {
      // Guts moves towards Light and bounces with momentum
      const dx = this.light.x - this.guts.x;
      const dy = this.light.y - this.guts.y;
      const dist = Math.hypot(dx, dy);

      const chaseForce = 140 * this.config.gutsSpeed;
      if (dist > 10) {
        this.guts.vx += (dx / dist) * chaseForce * dt;
        this.guts.vy += (dy / dist) * chaseForce * dt;
      }

      // Add a slight orbital drift so sword circles around Light
      this.guts.vx += (-dy / (dist + 50)) * 60 * dt;
      this.guts.vy += (dx / (dist + 50)) * 60 * dt;

      // Friction
      this.guts.vx *= Math.pow(0.92, dt * 60);
      this.guts.vy *= Math.pow(0.92, dt * 60);

      this.guts.x += this.guts.vx * dt;
      this.guts.y += this.guts.vy * dt;

      // Bounce off walls
      const minX = ARENA.x + 36;
      const maxX = ARENA.x + ARENA.width - 36;
      const minY = ARENA.y + 40;
      const maxY = ARENA.y + ARENA.height - 40;

      if (this.guts.x <= minX) { this.guts.x = minX; this.guts.vx = Math.abs(this.guts.vx); }
      if (this.guts.x >= maxX) { this.guts.x = maxX; this.guts.vx = -Math.abs(this.guts.vx); }
      if (this.guts.y <= minY) { this.guts.y = minY; this.guts.vy = Math.abs(this.guts.vy); }
      if (this.guts.y >= maxY) { this.guts.y = maxY; this.guts.vy = -Math.abs(this.guts.vy); }
    }

    runLightEvasionAI(dt) {
      // Light runs away from Guts and specifically avoids the sweeping sword arc
      const dx = this.light.x - this.guts.x;
      const dy = this.light.y - this.guts.y;
      const dist = Math.hypot(dx, dy);

      const fleeForce = 160 * this.config.lightSpeed;
      if (dist > 1) {
        this.light.vx += (dx / dist) * fleeForce * dt;
        this.light.vy += (dy / dist) * fleeForce * dt;
      }

      // Avoid corners / center seeking when trapped
      const cx = ARENA.x + ARENA.width / 2;
      const cy = ARENA.y + ARENA.height / 2;
      const distFromCenter = Math.hypot(this.light.x - cx, this.light.y - cy);
      if (distFromCenter > ARENA.width * 0.38) {
        this.light.vx += ((cx - this.light.x) / distFromCenter) * 70 * dt;
        this.light.vy += ((cy - this.light.y) / distFromCenter) * 70 * dt;
      }

      // Friction
      this.light.vx *= Math.pow(0.90, dt * 60);
      this.light.vy *= Math.pow(0.90, dt * 60);

      this.light.x += this.light.vx * dt;
      this.light.y += this.light.vy * dt;

      // Bounce off walls
      const minX = ARENA.x + 36;
      const maxX = ARENA.x + ARENA.width - 36;
      const minY = ARENA.y + 40;
      const maxY = ARENA.y + ARENA.height - 40;

      if (this.light.x <= minX) { this.light.x = minX; this.light.vx = Math.abs(this.light.vx) * 0.8; }
      if (this.light.x >= maxX) { this.light.x = maxX; this.light.vx = -Math.abs(this.light.vx) * 0.8; }
      if (this.light.y <= minY) { this.light.y = minY; this.light.vy = Math.abs(this.light.vy) * 0.8; }
      if (this.light.y >= maxY) { this.light.y = maxY; this.light.vy = -Math.abs(this.light.vy) * 0.8; }
    }

    handleCinematicHits() {
      // Check cinematic timeline
      for (const h of this.cinematicHits) {
        if (!h.dealt && this.matchTime >= h.time) {
          h.dealt = true;
          this.applyDamageToLight(10);
        }
      }
    }

    handlePhysicsSwordCollision() {
      if (this.guts.hitCooldown > 0) return;

      const swordLen = this.guts.swordBaseLength * this.config.swordLengthMult;
      const swordStartX = this.guts.x;
      const swordStartY = this.guts.y;
      const swordEndX = this.guts.x + Math.cos(this.guts.swordAngle) * swordLen;
      const swordEndY = this.guts.y + Math.sin(this.guts.swordAngle) * swordLen;

      const hit = this.checkSwordHit(
        swordStartX,
        swordStartY,
        swordEndX,
        swordEndY,
        this.light.x,
        this.light.y,
        this.light.radius
      );

      if (hit) {
        this.applyDamageToLight(10);
        this.guts.hitCooldown = 0.55 / this.config.swordSpinSpeed; // prevent multi-hit on same rotation
      }
    }

    applyDamageToLight(damage = 10) {
      this.light.hp = Math.max(0, this.light.hp - damage);
      this.light.flashTimer = 0.12;
      this.stats.hitsLanded += 1;

      // Spawn damage text above Light
      this.damageTexts.push(new DamageText(this.light.x + 55, this.light.y, `-${damage}`, '#ffd700'));

      // SFX & Sparks
      sound.playSwordHit();
      this.shake(6, 0.2);
      this.spawnHitSparks(this.light.x, this.light.y, 18);

      // Check if Guts Wins (Light HP = 0)
      if (this.light.hp <= 0) {
        this.triggerGutsVictory();
      }
    }

    triggerLightDeathNote() {
      this.light.state = 'pointing';
      this.guts.state = 'dead';
      this.guts.hp = 0;
      this.guts.flashTimer = 0.4;

      // Large -100 Damage text
      this.damageTexts.push(new DamageText(this.guts.x - 40, this.guts.y, '-100', '#ffd700', true));

      // Climax Sound & Massive screen shake
      sound.playDeathNoteClimax();
      this.shake(14, 0.6);
      this.spawnBloodDisintegrate(this.guts.x, this.guts.y, 60);

      this.winner = 'light';
      this.stats.lightWins += 1;
      this.stats.totalMatches += 1;

      setTimeout(() => {
        this.showWinnerModal('light');
        if (this.autoRestart) {
          setTimeout(() => this.resetMatch(), 2800);
        }
      }, 900);
    }

    triggerGutsVictory() {
      this.light.state = 'dead';
      this.winner = 'guts';
      this.stats.gutsWins += 1;
      this.stats.totalMatches += 1;

      sound.playDeathNoteClimax();
      this.shake(10, 0.4);
      this.spawnBloodDisintegrate(this.light.x, this.light.y, 50);

      setTimeout(() => {
        this.showWinnerModal('guts');
        if (this.autoRestart) {
          setTimeout(() => this.resetMatch(), 2800);
        }
      }, 900);
    }

    showWinnerModal(winner) {
      const modal = document.getElementById('winner-overlay');
      const title = document.getElementById('winner-title');
      const desc = document.getElementById('winner-desc');
      const emblem = document.getElementById('winner-emblem');
      const stats = document.getElementById('winner-stats-summary');

      if (winner === 'light') {
        emblem.textContent = '📓';
        title.textContent = 'LIGHT YAGAMI WINS!';
        title.style.color = '#f5c518';
        desc.textContent = 'Death Note completed! Guts was eliminated instantly (-100 HP).';
        stats.textContent = `Light Survived: ${this.matchTime.toFixed(1)}s | Remaining HP: ${this.light.hp}`;
      } else {
        emblem.textContent = '🗡️';
        title.textContent = 'GUTS WINS!';
        title.style.color = '#ff3344';
        desc.textContent = 'Dragon Slayer pierced through Light before the Death Note could be finished!';
        stats.textContent = `Battle Duration: ${this.matchTime.toFixed(1)}s | Guts HP: ${this.guts.hp}`;
      }

      modal.classList.remove('hidden');
      this.updateHUD();
    }

    hideWinnerModal() {
      const modal = document.getElementById('winner-overlay');
      modal.classList.add('hidden');
    }

    updateHUD() {
      // Match Timer Display
      const timerEl = document.getElementById('hud-timer');
      const totalSec = this.matchTime;
      const mins = Math.floor(totalSec / 60);
      const secs = (totalSec % 60).toFixed(1);
      timerEl.textContent = `${String(mins).padStart(2, '0')}:${String(secs).padStart(4, '0')}`;

      // Stats
      document.getElementById('stat-light-wins').textContent = this.stats.lightWins;
      document.getElementById('stat-guts-wins').textContent = this.stats.gutsWins;
      document.getElementById('stat-total-matches').textContent = this.stats.totalMatches;
      document.getElementById('stat-hits-landed').textContent = this.stats.hitsLanded;
    }

    draw() {
      ctx.save();

      // Handle Screen Shake
      if (this.shakeDuration > 0) {
        const sx = (Math.random() * 2 - 1) * this.shakeIntensity;
        const sy = (Math.random() * 2 - 1) * this.shakeIntensity;
        ctx.translate(sx, sy);
      }

      // 1. Draw Outer Sage Green Background
      ctx.fillStyle = ARENA.outerBg;
      ctx.fillRect(0, 0, V_WIDTH, V_HEIGHT);

      // 2. Draw Top Battle Title: "Light VS Guts"
      this.drawTitle();

      // 3. Draw Arena Box (Square with thick black border)
      this.drawArenaBox();

      // 4. Draw Characters & Sword
      if (this.guts.state === 'alive') {
        this.drawGuts();
        this.drawSword();
      }

      if (this.light.state !== 'dead') {
        this.drawLight();
      }

      // 5. Draw Particles & Damage Numbers
      for (const p of this.particles) p.draw(ctx);
      for (const d of this.damageTexts) d.draw(ctx);

      // 6. Draw Hitboxes if enabled
      if (this.showHitboxes) {
        this.drawHitboxes();
      }

      ctx.restore();
    }

    drawTitle() {
      ctx.save();
      const titleY = ARENA.y - 32;

      ctx.font = '700 38px Montserrat, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      // "Light"
      ctx.fillStyle = '#000000';
      ctx.fillText('Light', V_WIDTH / 2 - 95, titleY);

      // "VS" (with white fill and grey outline)
      ctx.font = '900 36px Orbitron, sans-serif';
      ctx.strokeStyle = '#6b7280';
      ctx.lineWidth = 3;
      ctx.strokeText('VS', V_WIDTH / 2 - 10, titleY);
      ctx.fillStyle = '#ffffff';
      ctx.fillText('VS', V_WIDTH / 2 - 10, titleY);

      // "Guts"
      ctx.font = '700 38px Montserrat, sans-serif';
      ctx.fillStyle = '#000000';
      ctx.fillText('Guts', V_WIDTH / 2 + 75, titleY);

      ctx.restore();
    }

    drawArenaBox() {
      ctx.save();

      // Thick Black Outer Border
      ctx.strokeStyle = ARENA.borderColor;
      ctx.lineWidth = ARENA.borderWidth;
      ctx.strokeRect(ARENA.x, ARENA.y, ARENA.width, ARENA.height);

      // Arena Interior Solid Grey
      ctx.fillStyle = ARENA.bgColor;
      ctx.fillRect(ARENA.x, ARENA.y, ARENA.width, ARENA.height);

      // Subtle Watermark "@Boxx_Arena" in arena
      ctx.font = '800 34px Montserrat, sans-serif';
      ctx.fillStyle = 'rgba(255, 255, 255, 0.09)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('@Boxx_Arena', ARENA.x + ARENA.width / 2, ARENA.y + ARENA.height / 2 + 30);

      ctx.restore();
    }

    drawGuts() {
      ctx.save();
      ctx.translate(this.guts.x, this.guts.y);

      // Draw Guts Sprite
      const sprite = this.guts.flashTimer > 0 ? assets.gutsFlash : assets.guts;
      if (sprite.complete && sprite.naturalWidth > 0) {
        const w = 110;
        const h = 100;
        ctx.drawImage(sprite, -w / 2, -h / 2, w, h);
      } else {
        // Fallback vector circle
        ctx.fillStyle = '#222';
        ctx.beginPath();
        ctx.arc(0, 0, this.guts.radius, 0, Math.PI * 2);
        ctx.fill();
      }

      // Draw Health Bar
      this.drawHealthBar(0, 58, this.guts.hp, this.guts.maxHp, false);

      ctx.restore();
    }

    drawSword() {
      ctx.save();
      ctx.translate(this.guts.x, this.guts.y);
      ctx.rotate(this.guts.swordAngle);

      // Sword Motion Blur Trail Arc
      ctx.beginPath();
      ctx.fillStyle = 'rgba(255, 255, 255, 0.12)';
      const trailAngle = 0.45;
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, this.guts.swordBaseLength * this.config.swordLengthMult, -trailAngle, 0, false);
      ctx.closePath();
      ctx.fill();

      // Draw Dragon Slayer Sprite
      if (assets.sword.complete && assets.sword.naturalWidth > 0) {
        const sw = 220 * this.config.swordLengthMult;
        const sh = 42;
        // Sword image has hilt at left (0,0) and blade pointing right
        ctx.drawImage(assets.sword, 0, -sh / 2, sw, sh);
      } else {
        // Fallback vector blade
        ctx.fillStyle = '#111';
        ctx.strokeStyle = '#bbb';
        ctx.lineWidth = 3;
        const sw = this.guts.swordBaseLength * this.config.swordLengthMult;
        ctx.fillRect(0, -12, sw, 24);
        ctx.strokeRect(0, -12, sw, 24);
      }

      ctx.restore();
    }

    drawLight() {
      ctx.save();
      ctx.translate(this.light.x, this.light.y);

      // Select Sprite: Flash -> Pointing -> Writing
      let sprite = assets.lightWriting;
      if (this.light.flashTimer > 0) {
        sprite = assets.lightFlash;
      } else if (this.light.state === 'pointing') {
        sprite = assets.lightPointing;
      }

      if (sprite.complete && sprite.naturalWidth > 0) {
        const w = this.light.state === 'pointing' ? 120 : 90;
        const h = 110;
        ctx.drawImage(sprite, -w / 2, -h / 2, w, h);
      } else {
        // Fallback vector circle
        ctx.fillStyle = '#fff';
        ctx.beginPath();
        ctx.arc(0, 0, this.light.radius, 0, Math.PI * 2);
        ctx.fill();
      }

      // Draw Health Bar
      this.drawHealthBar(0, 58, this.light.hp, this.light.maxHp, false);

      // Draw Death Note Countdown Timer Bar (Below HP Bar)
      if (this.light.state === 'writing') {
        this.drawCountdownBar(0, 84, this.light.dnTimer, this.light.maxDnTimer);
      }

      ctx.restore();
    }

    // Health Bar: Red filled bar with black border and numerical label
    drawHealthBar(x, y, hp, maxHp, isCompact = false) {
      ctx.save();
      const barW = 115;
      const barH = 26;
      const left = x - barW / 2;
      const top = y;

      // Background (White / Empty)
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(left, top, barW, barH);

      // Red Health Fill
      const pct = Math.max(0, Math.min(1, hp / maxHp));
      ctx.fillStyle = '#e60000';
      ctx.fillRect(left, top, barW * pct, barH);

      // Black Border
      ctx.strokeStyle = '#000000';
      ctx.lineWidth = 2.5;
      ctx.strokeRect(left, top, barW, barH);

      // Health Text (Bold Center)
      ctx.font = '900 18px Montserrat, Arial, sans-serif';
      ctx.fillStyle = '#000000';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(Math.round(hp).toString(), x, top + barH / 2 + 1);

      ctx.restore();
    }

    // Countdown Timer Bar for Light (White/Grey fill with comma format: 14,5)
    drawCountdownBar(x, y, timer, maxTimer) {
      ctx.save();
      const barW = 115;
      const barH = 24;
      const left = x - barW / 2;
      const top = y;

      // Background (White)
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(left, top, barW, barH);

      // Grey/Blue Progress Fill (drains as time elapses)
      const pct = Math.max(0, Math.min(1, timer / maxTimer));
      ctx.fillStyle = '#b0b5be';
      ctx.fillRect(left, top, barW * (1 - pct), barH);

      // Black Border
      ctx.strokeStyle = '#000000';
      ctx.lineWidth = 2.5;
      ctx.strokeRect(left, top, barW, barH);

      // Timer Text in comma format like the video: "14,5", "12,9"
      const formatted = timer.toFixed(1).replace('.', ',');
      ctx.font = '800 16px Montserrat, Arial, sans-serif';
      ctx.fillStyle = '#000000';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(formatted, x, top + barH / 2 + 1);

      ctx.restore();
    }

    drawHitboxes() {
      ctx.save();
      ctx.lineWidth = 2;

      // Light Hitbox Circle
      ctx.strokeStyle = '#00ffff';
      ctx.beginPath();
      ctx.arc(this.light.x, this.light.y, this.light.radius, 0, Math.PI * 2);
      ctx.stroke();

      // Guts Hitbox Circle
      ctx.strokeStyle = '#ff00ff';
      ctx.beginPath();
      ctx.arc(this.guts.x, this.guts.y, this.guts.radius, 0, Math.PI * 2);
      ctx.stroke();

      // Sword Hitbox Line
      const swordLen = this.guts.swordBaseLength * this.config.swordLengthMult;
      const sx = this.guts.x + Math.cos(this.guts.swordAngle) * swordLen;
      const sy = this.guts.y + Math.sin(this.guts.swordAngle) * swordLen;
      ctx.strokeStyle = '#ffff00';
      ctx.beginPath();
      ctx.moveTo(this.guts.x, this.guts.y);
      ctx.lineTo(sx, sy);
      ctx.stroke();

      ctx.restore();
    }
  }

  // Initialize Game Instance
  const battle = new BattleEngine();

  // Animation Loop
  let lastTime = performance.now();
  function gameLoop(currentTime) {
    const dt = Math.min(0.1, (currentTime - lastTime) / 1000);
    lastTime = currentTime;

    battle.update(dt);
    battle.draw();

    requestAnimationFrame(gameLoop);
  }
  requestAnimationFrame(gameLoop);

  // UI Bindings & Event Listeners
  const btnPlayPause = document.getElementById('btn-play-pause');
  const btnRestart = document.getElementById('btn-restart');
  const btnModalRestart = document.getElementById('btn-modal-restart');
  const modeButtons = document.querySelectorAll('.mode-btn');
  const speedButtons = document.querySelectorAll('.btn-chip');
  const chkAutoRestart = document.getElementById('chk-auto-restart');
  const chkHitboxes = document.getElementById('chk-hitboxes');
  const sandboxPanel = document.getElementById('sandbox-panel');
  const playGuide = document.getElementById('play-guide');
  const guideText = document.getElementById('guide-text');
  const hudModeBadge = document.getElementById('hud-mode-badge');
  const touchControls = document.getElementById('touch-controls');

  // Play / Pause Button
  btnPlayPause.addEventListener('click', () => {
    sound.init();
    if (!battle.running) {
      battle.running = true;
      battle.paused = false;
      document.getElementById('play-text').textContent = 'PAUSE';
      btnPlayPause.querySelector('.btn-icon').textContent = '⏸';
    } else {
      battle.paused = !battle.paused;
      document.getElementById('play-text').textContent = battle.paused ? 'RESUME' : 'PAUSE';
      btnPlayPause.querySelector('.btn-icon').textContent = battle.paused ? '▶' : '⏸';
    }
  });

  // Restart Button
  btnRestart.addEventListener('click', () => {
    sound.init();
    battle.resetMatch();
    battle.running = true;
    battle.paused = false;
    document.getElementById('play-text').textContent = 'PAUSE';
    btnPlayPause.querySelector('.btn-icon').textContent = '⏸';
  });

  btnModalRestart.addEventListener('click', () => {
    sound.init();
    battle.resetMatch();
    battle.running = true;
    battle.paused = false;
    document.getElementById('play-text').textContent = 'PAUSE';
    btnPlayPause.querySelector('.btn-icon').textContent = '⏸';
  });

  // Mode Selection
  modeButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      sound.init();
      modeButtons.forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');

      const mode = btn.dataset.mode;
      battle.mode = mode;

      // Update HUD mode badge
      hudModeBadge.textContent = btn.querySelector('strong').textContent.toUpperCase();

      // Show/Hide Sandbox Settings
      if (mode === 'sandbox') {
        sandboxPanel.classList.remove('hidden');
      } else {
        sandboxPanel.classList.add('hidden');
      }

      // Show/Hide Controls Guide
      if (mode === 'play-guts') {
        playGuide.classList.remove('hidden');
        guideText.textContent = 'Playing Guts: Move with WASD/Arrows/Mouse to slash Light!';
      } else if (mode === 'play-light') {
        playGuide.classList.remove('hidden');
        guideText.textContent = 'Playing Light: Move with WASD/Arrows/Mouse to dodge for 15s!';
      } else {
        playGuide.classList.add('hidden');
      }

      battle.resetMatch();
      battle.running = true;
      battle.paused = false;
      document.getElementById('play-text').textContent = 'PAUSE';
      btnPlayPause.querySelector('.btn-icon').textContent = '⏸';
    });
  });

  // Speed Chips
  speedButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      speedButtons.forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      battle.speedMultiplier = parseFloat(btn.dataset.speed);
    });
  });

  // Toggles
  chkAutoRestart.addEventListener('change', (e) => {
    battle.autoRestart = e.target.checked;
  });

  chkHitboxes.addEventListener('change', (e) => {
    battle.showHitboxes = e.target.checked;
  });

  // Sandbox Range Sliders
  const bindSlider = (id, valId, key, formatFn) => {
    const input = document.getElementById(id);
    const label = document.getElementById(valId);
    if (!input || !label) return;
    input.addEventListener('input', (e) => {
      const val = parseFloat(e.target.value);
      battle.config[key] = val;
      label.textContent = formatFn ? formatFn(val) : val;
      battle.resetMatch();
    });
  };

  bindSlider('param-dn-time', 'val-dn-time', 'deathNoteTime', (v) => `${v.toFixed(1)}s`);
  bindSlider('param-sword-len', 'val-sword-len', 'swordLengthMult', (v) => `${v.toFixed(1)}x`);
  bindSlider('param-spin-spd', 'val-spin-spd', 'swordSpinSpeed', (v) => `${v.toFixed(1)}x`);
  bindSlider('param-guts-spd', 'val-guts-spd', 'gutsSpeed', (v) => `${v.toFixed(1)}x`);
  bindSlider('param-light-spd', 'val-light-spd', 'lightSpeed', (v) => `${v.toFixed(1)}x`);
  bindSlider('param-light-hp', 'val-light-hp', 'lightMaxHP', (v) => `${v}`);
  bindSlider('param-guts-hp', 'val-guts-hp', 'gutsMaxHP', (v) => `${v}`);

  // Reset Stats Button
  document.getElementById('btn-reset-stats').addEventListener('click', () => {
    battle.stats.lightWins = 0;
    battle.stats.gutsWins = 0;
    battle.stats.totalMatches = 0;
    battle.stats.hitsLanded = 0;
    battle.updateHUD();
  });

  // Sound & BGM Toggles
  const btnToggleSound = document.getElementById('btn-toggle-sound');
  const btnToggleBGM = document.getElementById('btn-toggle-bgm');

  btnToggleSound.addEventListener('click', () => {
    sound.init();
    sound.enabled = !sound.enabled;
    document.getElementById('sound-icon').textContent = sound.enabled ? '🔊' : '🔇';
    document.getElementById('sound-text').textContent = sound.enabled ? 'Sound Effects: ON' : 'Sound Effects: OFF';
  });

  btnToggleBGM.addEventListener('click', () => {
    sound.init();
    const isPlaying = sound.toggleBGM();
    document.getElementById('bgm-icon').textContent = isPlaying ? '🎶' : '🎵';
    document.getElementById('bgm-text').textContent = isPlaying ? 'Original BGM: ON' : 'Original BGM: OFF';
  });

  // Auto start on page load
  battle.running = true;
  document.getElementById('play-text').textContent = 'PAUSE';
  btnPlayPause.querySelector('.btn-icon').textContent = '⏸';

})();
