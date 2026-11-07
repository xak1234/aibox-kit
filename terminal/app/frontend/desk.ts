/** Pixel geometry stays on the integer grid; animation follows actual agent state. */
export function startDesk(canvas: HTMLCanvasElement, status: () => string): void {
  const context = canvas.getContext('2d');
  if (!context) throw new Error('Canvas is unavailable.');
  const ctx = context;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let last = 0;
  function draw(t: number): void {
    if (t - last < 100) { requestAnimationFrame(draw); return; }
    last = t;
    const phase = status();
    const busy = phase === 'working';
    const awake = phase !== 'offline';
    const bob = !reduced && busy ? Math.floor(t / 280) % 2 * 4 : 0;
    const r = (x: number, y: number, w: number, h: number, color: string): void => { ctx.fillStyle = color; ctx.fillRect(x, y, w, h); };
    ctx.imageSmoothingEnabled = false;
    r(0, 0, 800, 440, '#1b2118');
    // Quiet wall and floor, drawn at the same resolution every frame.
    r(0, 340, 800, 100, '#191e15'); r(0, 340, 800, 4, '#26301e');
    for (let x = 0; x < 800; x += 80) r(x, 344, 2, 96, '#1f271a');
    r(0, 388, 800, 2, '#232b1d');
    for (let x = 35; x < 790; x += 20) for (let y = 70; y < 320; y += 20) r(x, y, 2, 2, '#2b3423');
    // Window, distant night and a few fixed stars.
    r(615, 65, 116, 143, '#303e28'); r(621, 71, 104, 131, '#121b18');
    r(626, 76, 94, 121, '#17251f'); r(673, 74, 4, 126, '#3b4b31'); r(623, 134, 100, 4, '#3b4b31');
    r(636, 93, 3, 3, '#7d9475'); r(700, 112, 3, 3, '#899e7c'); r(693, 84, 11, 11, '#b8c79b');
    r(690, 81, 9, 9, '#17251f'); r(630, 181, 24, 16, '#223927'); r(684, 176, 33, 21, '#25392b');
    r(607, 207, 132, 7, '#536144'); r(615, 214, 116, 5, '#303f25');
    // Shelf with books and a small speaker.
    r(73, 134, 106, 8, '#526143'); r(81, 142, 7, 12, '#37472d'); r(162, 142, 7, 12, '#37472d');
    r(85, 100, 11, 34, '#a68560'); r(98, 94, 9, 40, '#718260'); r(109, 103, 14, 31, '#b2b68c');
    r(87, 106, 7, 3, '#d0b391'); r(100, 100, 5, 3, '#a2b58b'); r(111, 108, 10, 3, '#d1d6b2');
    r(138, 109, 27, 25, '#3b4931'); r(145, 115, 13, 13, '#1b2517'); r(150, 120, 3, 3, '#77866a');
    // Desk shadow.
    r(158, 397, 475, 10, '#11170f'); r(180, 407, 440, 7, '#141b11');
    // Monitor chassis; the live xterm surface is composited over its exact screen rectangle.
    r(220, 84, 332, 184, '#161d12'); r(216, 88, 340, 176, '#526042');
    r(220, 92, 332, 168, '#3b4a2f'); r(224, 96, 324, 164, '#0d130c');
    r(228, 100, 312, 156, '#0b100b'); r(220, 264, 332, 8, '#38472c');
    r(380, 264, 12, 4, awake ? '#c4dca3' : '#657357');
    r(365, 272, 40, 20, '#38462d'); r(355, 288, 60, 8, '#566346');
    // Lamp on the left, warm cone implied by small highlights.
    r(169, 219, 8, 77, '#84936d'); r(157, 290, 34, 8, '#788967');
    r(173, 215, 39, 8, '#7e8e6a'); r(199, 218, 27, 7, '#aeb589');
    r(194, 225, 36, 8, '#c9c59a'); r(198, 233, 28, 4, '#d6cd9c');
    r(196, 244, 30, 2, '#504e2c'); r(190, 256, 40, 2, '#3c4124');
    // Plant right of the monitor.
    r(603, 279, 34, 19, '#9c7958'); r(607, 298, 26, 5, '#72563b');
    r(599, 275, 42, 7, '#bb9168'); r(617, 239, 5, 38, '#5c7541');
    r(602, 235, 17, 11, '#819b51'); r(594, 232, 12, 9, '#6b8846'); r(620, 247, 20, 10, '#78964d');
    r(635, 241, 9, 10, '#8eaa5a'); r(608, 254, 12, 8, '#536e37'); r(616, 222, 8, 20, '#90a85c');
    // Wood worktop and legs.
    r(141, 300, 520, 12, '#8c906b'); r(145, 312, 512, 10, '#62694c');
    r(157, 322, 12, 77, '#47533a'); r(161, 322, 5, 73, '#627052');
    r(633, 322, 12, 77, '#47533a'); r(633, 322, 5, 73, '#627052');
    r(185, 300, 140, 3, '#a5a47a'); r(510, 303, 100, 3, '#a5a47a');
    // Keyboard and mug.
    r(316, 288, 110, 12, '#8a9272'); r(322, 288, 98, 3, '#b5bc96');
    for (let x = 324; x < 420; x += 8) r(x, 292, 4, 3, '#5b694d');
    r(526, 281, 23, 19, '#c0af83'); r(549, 285, 8, 11, '#a69972'); r(551, 288, 3, 5, '#27301f');
    r(528, 278, 19, 5, '#6d5c40');
    // Operator sits in front of the keyboard; hands move only for real execution.
    r(420, 356, 65, 13, '#2b3622'); r(426, 369, 9, 30, '#37472d'); r(473, 369, 9, 30, '#37472d');
    r(432, 353, 16, 38, '#68795a'); r(462, 352, 16, 39, '#637453');
    r(428, 387, 24, 12, '#a7ae87'); r(459, 387, 26, 12, '#8f9b76');
    r(420, 306, 68, 51, '#c3c8a0'); r(416, 314, 76, 32, '#c3c8a0');
    r(466, 310, 22, 47, '#8d9d75'); r(424, 348, 43, 9, '#a5b38b');
    r(430, 298, 47, 12, '#81946b');
    r(423, 258, 61, 42, '#d7d8b5'); r(419, 266, 69, 27, '#d7d8b5');
    r(466, 262, 18, 38, '#a8b58e'); r(428, 256, 51, 5, '#e7e5c4');
    r(418, 270, 35, 18, '#3d5137'); r(422, 273, 28, 12, '#273a2a');
    r(425, 276, 5, 4, awake ? '#c3e697' : '#758a60'); r(440, 276, 5, 4, awake ? '#c3e697' : '#758a60');
    r(453, 248, 4, 10, '#839970'); r(449, 244, 12, 5, awake ? '#d0e7a5' : '#758960');
    r(402, 312, 23, 12, '#aaba8f'); r(387, 302 + bob, 18, 15, '#c5cba5');
    r(372, 292 + bob, 24, 12, '#d8dbb6'); r(479, 317, 13, 24, '#7c9167');
    r(433, 323, 22, 17, '#94a57a'); r(438, 328, 12, 7, '#607750');
    // A discreet status lamp, bound to actual state.
    r(730, 329, 5, 5, awake ? '#a7c582' : '#435139');
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
}
