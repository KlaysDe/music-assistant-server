import NextBtn from "@/layouts/default/PlayerOSD/PlayerControlBtn/NextBtn.vue";
import api from "@/plugins/api";
import type {
  MediaItemChapter,
  Player,
  PlayerQueue,
  QueueItem,
} from "@/plugins/api/interfaces";
import { store } from "@/plugins/store";
import { enableAutoUnmount, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { audioSource } from "../fixtures/audioSource";
import { playerQueue } from "../fixtures/playerQueue";
import { playerSource } from "../fixtures/playerSource";
import { queueItem } from "../fixtures/queueItem";

const chapter = vi.hoisted(() => ({
  next: undefined as MediaItemChapter | undefined,
}));

vi.mock("@/composables/useChapterNavigation", async () => {
  const { computed } = await vi.importActual<typeof import("vue")>("vue");
  return {
    useChapterNavigation: () => ({ nextChapter: computed(() => chapter.next) }),
  };
});

vi.mock("@/plugins/api", () => ({
  default: { playerCommandNext: vi.fn(), playerCommandSeek: vi.fn() },
}));
vi.mock("@/plugins/store", () => ({ store: {} }));

const mockStore = store as unknown as { curQueueItem?: QueueItem };

function mountButton({
  queue = playerQueue({ items: 5 }),
  activeSource = queue.queue_id,
}: { queue?: PlayerQueue; activeSource?: string | null } = {}) {
  const source = playerSource({
    id: activeSource || queue.queue_id,
    can_next_previous: true,
  });
  const player = {
    player_id: queue.queue_id,
    active_source: activeSource,
    source_list: [source],
  } as Player;
  return mount(NextBtn, {
    props: { player, playerQueue: queue },
    global: {
      mocks: { $t: (key: string) => key },
      stubs: { VIcon: true, VBadge: true },
    },
  });
}

const workingQueue = playerQueue({
  items: 5,
  extra_attributes: { play_action_in_progress: true },
});

enableAutoUnmount(afterEach);

beforeEach(() => {
  vi.clearAllMocks();
  chapter.next = undefined;
  mockStore.curQueueItem = undefined;
});

describe("NextBtn", () => {
  it.each(["queue-1", null])(
    "continues sending next commands during playback work with source %s",
    async (activeSource) => {
      const wrapper = mountButton({ activeSource });
      const button = wrapper.get('[role="button"]');
      await button.trigger("click");
      await wrapper.setProps({ playerQueue: workingQueue });
      expect(button.attributes("aria-disabled")).toBe("false");
      await button.trigger("click");
      await button.trigger("click");
      expect(api.playerCommandNext).toHaveBeenCalledTimes(3);
      expect(api.playerCommandNext).toHaveBeenLastCalledWith("queue-1");
    },
  );

  it("keeps chapter seeking disabled during playback work", async () => {
    chapter.next = { position: 2, name: "Chapter 2", start: 60, end: 120 };
    const wrapper = mountButton({ queue: workingQueue });
    const button = wrapper.get('[role="button"]');
    expect(button.attributes("aria-disabled")).toBe("true");
    await button.trigger("click");
    expect(api.playerCommandSeek).not.toHaveBeenCalled();
  });

  it("keeps an external player source disabled during playback work", async () => {
    const wrapper = mountButton({
      queue: workingQueue,
      activeSource: "spotify://audio_source/main",
    });
    const button = wrapper.get('[role="button"]');
    expect(button.attributes("aria-disabled")).toBe("true");
    await button.trigger("click");
    expect(api.playerCommandNext).not.toHaveBeenCalled();
  });

  it("keeps external audio queue items disabled during playback work", async () => {
    mockStore.curQueueItem = queueItem({
      media_item: audioSource({ can_next_previous: true }),
    });
    const wrapper = mountButton({ queue: workingQueue });
    const button = wrapper.get('[role="button"]');
    expect(button.attributes("aria-disabled")).toBe("true");
    await button.trigger("click");
    expect(api.playerCommandNext).not.toHaveBeenCalled();
  });
});
