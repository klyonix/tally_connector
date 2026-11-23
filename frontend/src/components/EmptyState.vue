<template>
  <div class="flex flex-col items-center justify-center gap-2 rounded border border-dashed border-outline-gray-2 px-6 py-10 text-center">
    <p class="text-base font-medium text-ink-gray-7">{{ title }}</p>
    <p v-if="description" class="max-w-md text-p-sm text-ink-gray-5">{{ description }}</p>
    <div v-if="$slots.action" class="mt-2"><slot name="action" /></div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>
