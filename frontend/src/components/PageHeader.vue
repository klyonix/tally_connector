<template>
  <header class="flex flex-wrap items-start justify-between gap-3 pb-4">
    <div class="min-w-0">
      <h1 class="text-lg font-semibold text-ink-gray-9">{{ title }}</h1>
      <p v-if="subtitle" class="mt-0.5 text-p-sm text-ink-gray-6">{{ subtitle }}</p>
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup lang="ts">
defineProps<{ title: string; subtitle?: string }>()
</script>
