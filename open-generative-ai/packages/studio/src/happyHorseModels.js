import { createGroupedVideoRegistry } from "./groupedVideoRegistry.js";

export const HAPPY_HORSE_FAMILY_NAMES = Object.freeze({
  "happy-horse-1.1": "Happy Horse 1.1",
  "happy-horse-1": "Happy Horse 1.0",
});

const registry = createGroupedVideoRegistry({
  facetOptions: {},
  facetLabels: {},
  registerVariants(register) {
    for (const familyId of Object.keys(HAPPY_HORSE_FAMILY_NAMES)) {
      register(`${familyId}-text-to-video`, familyId, [null]);
      register(`${familyId}-image-to-video`, familyId, ["animate_image"]);
      register(`${familyId}-reference-to-video`, familyId, ["references"]);
      register(`${familyId}-video-edit`, familyId, ["edit_video"]);
    }
  },
});

export const {
  getConfiguration: getHappyHorseConfiguration,
  getVariants: getHappyHorseVariants,
  resolveVariant: resolveHappyHorseVariant,
} = registry;

export const HAPPY_HORSE_MODEL_GROUP = Object.freeze({
  copyKey: "happyHorse",
  familyNames: HAPPY_HORSE_FAMILY_NAMES,
  configurations: registry.configurations,
  workflowVariants: registry.workflowVariants,
  resolveVariant: resolveHappyHorseVariant,
  getVariantOptions: registry.getVariantOptions,
});
