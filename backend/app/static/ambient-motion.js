(function startAmbientMotion() {
  'use strict';

  const canvas = document.getElementById('scifi-canvas');
  if (!canvas) return;
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    canvas.hidden = true;
    return;
  }

  const context = canvas.getContext('2d', { alpha: true });
  if (!context) return;

  const mobile = window.matchMedia?.('(max-width: 760px)').matches;
  const particleCount = mobile ? 18 : 34;
  const linkDistance = mobile ? 86 : 108;
  const linkDistanceSquared = linkDistance * linkDistance;
  const frameInterval = 1000 / 30;
  let width = 0;
  let height = 0;
  let lastFrameAt = 0;
  let resizeFrame = 0;

  class Particle {
    constructor() {
      this.x = Math.random() * width;
      this.y = Math.random() * height;
      this.vx = (Math.random() - 0.5) * 0.38;
      this.vy = (Math.random() - 0.5) * 0.38;
      this.radius = Math.random() * 1.2 + 0.5;
    }

    update() {
      this.x += this.vx;
      this.y += this.vy;
      if (this.x < 0 || this.x > width) this.vx *= -1;
      if (this.y < 0 || this.y > height) this.vy *= -1;
    }

    draw() {
      context.beginPath();
      context.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
      context.fillStyle = 'rgba(62, 151, 255, 0.34)';
      context.fill();
    }
  }

  const particles = [];

  function resizeCanvas() {
    width = window.innerWidth;
    height = window.innerHeight;
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.max(1, Math.round(width * pixelRatio));
    canvas.height = Math.max(1, Math.round(height * pixelRatio));
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    if (!particles.length) {
      for (let index = 0; index < particleCount; index += 1) particles.push(new Particle());
    }
  }

  function drawFrame(timestamp) {
    window.requestAnimationFrame(drawFrame);
    if (document.hidden || timestamp - lastFrameAt < frameInterval) return;
    lastFrameAt = timestamp;
    context.clearRect(0, 0, width, height);

    for (let first = 0; first < particles.length; first += 1) {
      const particle = particles[first];
      particle.update();
      particle.draw();
      for (let second = first + 1; second < particles.length; second += 1) {
        const other = particles[second];
        const dx = particle.x - other.x;
        const dy = particle.y - other.y;
        const distanceSquared = dx * dx + dy * dy;
        if (distanceSquared >= linkDistanceSquared) continue;
        const opacity = 0.1 * (1 - Math.sqrt(distanceSquared) / linkDistance);
        context.beginPath();
        context.moveTo(particle.x, particle.y);
        context.lineTo(other.x, other.y);
        context.strokeStyle = `rgba(62, 151, 255, ${opacity})`;
        context.lineWidth = 0.5;
        context.stroke();
      }
    }
  }

  window.addEventListener('resize', () => {
    window.cancelAnimationFrame(resizeFrame);
    resizeFrame = window.requestAnimationFrame(resizeCanvas);
  }, { passive: true });
  resizeCanvas();
  window.requestAnimationFrame(drawFrame);
})();
