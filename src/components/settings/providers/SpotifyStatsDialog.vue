<template>
  <Dialog :open="!!frameUrl" @update:open="onOpenChange">
    <DialogTrigger as-child>
      <Button
        data-testid="provider-spotify-stats"
        variant="outline"
        :disabled="opening"
        :aria-busy="opening"
        @click="openStats"
      >
        <ChartNoAxesColumn class="size-4" />
        {{ label }}
      </Button>
    </DialogTrigger>
    <DialogContent
      :aria-describedby="undefined"
      class="h-[90dvh] grid-rows-[auto_minmax(0,1fr)] gap-0 overflow-hidden p-0 sm:max-w-[min(1200px,calc(100%-2rem))]"
    >
      <DialogHeader class="border-b px-4 py-5 pr-12">
        <DialogTitle>{{ $t("statistics") }}</DialogTitle>
      </DialogHeader>
      <iframe
        v-if="frameUrl"
        :src="frameUrl"
        :title="$t('statistics')"
        class="size-full border-0"
        referrerpolicy="no-referrer"
      ></iframe>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { api } from "@/plugins/api";
import { ChartNoAxesColumn } from "@lucide/vue";
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";

const props = defineProps<{
  instanceId: string;
  action: string;
  label: string;
}>();

const { t } = useI18n();
const opening = ref(false);
const frameUrl = ref<string | null>(null);

function onOpenChange(open: boolean) {
  if (!open) frameUrl.value = null;
}

async function openStats() {
  if (opening.value || frameUrl.value) return;
  opening.value = true;
  try {
    const result = await api.invokeProviderConfigAction(
      props.instanceId,
      props.action,
    );
    if (!Array.isArray(result) && result.open_url) {
      const url = URL.parse(result.open_url);
      const statsPath = `/spotify/stats/${encodeURIComponent(props.instanceId)}/`;
      if (
        url?.origin === window.location.origin &&
        url.pathname.endsWith(statsPath) &&
        url.hash
      ) {
        frameUrl.value = url.href;
        toast.success(result.message || t("settings.action_completed"));
        return;
      }
    }
    toast.error(t("settings.spotify_stats_unavailable"));
  } catch {
    // The API reports its own errors.
  } finally {
    opening.value = false;
  }
}
</script>
