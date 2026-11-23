<template>
	<!--
    The KlyONIX Tech mark, used as ground rather than as a badge.

    The two triangles are the brand file's own paths - the same geometry as the
    logo, translated out of its 375-unit artboard - so this is the mark enlarged
    and quietened, not a shape invented to look like it. The wedge between them
    is the negative space that makes the mark, so it stays a gap.

    Very low contrast on purpose: this sits behind figures somebody has to read.
    It is decoration, so it is hidden from assistive technology, and it stops
    animating for anyone who has asked for less motion.
  -->
	<div class="kx-backdrop" aria-hidden="true">
		<svg viewBox="0 0 400 200" preserveAspectRatio="xMaxYMid slice">
			<defs>
				<linearGradient id="kxMarkFade" x1="0" y1="0" x2="1" y2="1">
					<stop offset="0%" stop-color="#006d5b" stop-opacity=".9" />
					<stop offset="100%" stop-color="#006d5b" stop-opacity=".14" />
				</linearGradient>
			</defs>

			<!-- The mark, oversized and bled off the right edge. -->
			<g transform="translate(232 -18) scale(1.62)" fill="url(#kxMarkFade)">
				<path d="M 23.23 84.14 L 0.03 84.14 L 0.03 0 Z" />
				<path d="M 82.98 84.13 L 0.1 0.01 L 82.98 0.01 Z" />
			</g>

			<!-- The same mark again, smaller and fainter, as a rhythm rather than a
           repeat: the diagonal is the memorable part of the identity. -->
			<g transform="translate(150 96) scale(0.72)" fill="#006d5b" opacity=".12">
				<path d="M 23.23 84.14 L 0.03 84.14 L 0.03 0 Z" />
				<path d="M 82.98 84.13 L 0.1 0.01 L 82.98 0.01 Z" />
			</g>

			<!-- The chevron from the O of ONIX. One accent, in its own red, small
           enough to be a signature rather than a decoration. -->
			<g transform="translate(196 44)" opacity=".22">
				<path
					d="M 10 0 L 0 10 L 10 20"
					fill="none"
					stroke="#ff2400"
					stroke-width="2.4"
					stroke-linecap="round"
					stroke-linejoin="round"
				/>
				<path
					d="M 20 0 L 30 10 L 20 20"
					fill="none"
					stroke="#ff2400"
					stroke-width="2.4"
					stroke-linecap="round"
					stroke-linejoin="round"
				/>
			</g>

			<!-- The gold full stop after "Tech". -->
			<circle cx="243" cy="150" r="3.2" fill="#d4a017" opacity=".38" />
		</svg>
	</div>
</template>

<style scoped>
.kx-backdrop {
	position: absolute;
	inset: 0;
	overflow: hidden;
	border-radius: inherit;
	pointer-events: none;
}

.kx-backdrop svg {
	position: absolute;
	inset: 0;
	width: 100%;
	height: 100%;
	/* Strong enough to read as the mark, faint enough that the words in front
     stay the thing you look at. Judged on screen rather than picked. */
	opacity: 0.16;
}

/* The wash the mark sits on: the brand teal at almost nothing, so the panel
   reads as KlyONIX Tech rather than as a grey box with a logo behind it. */
.kx-backdrop::before {
	content: '';
	position: absolute;
	inset: 0;
	background: linear-gradient(
		105deg,
		var(--kx-brand-100) 0%,
		var(--kx-brand-50) 34%,
		transparent 72%
	);
}

@media (prefers-color-scheme: dark) {
	.kx-backdrop svg {
		opacity: 0.2;
	}
	.kx-backdrop::before {
		background: linear-gradient(105deg, rgba(0, 109, 91, 0.16) 0%, transparent 62%);
	}
}
</style>
