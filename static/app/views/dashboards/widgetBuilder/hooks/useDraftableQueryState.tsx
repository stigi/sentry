import {useCallback, useState} from 'react';
import {useQueryState, type UseQueryStateOptions} from 'nuqs';

type DraftOptions = {
  /**
   * When false, the value is held locally and the URL is left alone.
   */
  updateUrl?: boolean;
};

type SetDraftableValue<T> = (value: T | null | undefined, options?: DraftOptions) => void;

/**
 * What the parser makes of a param that isn't in the URL at all.
 */
function parseAbsent<T>(parser: UseQueryStateOptions<T>): T | null {
  return parser.type === 'multi' ? parser.parse([]) : parser.parse('');
}

/**
 * A nuqs query state that can hold an uncommitted value.
 *
 * The widget builder's text inputs are controlled by builder state, so every
 * keystroke would otherwise be a URL write. Passing `updateUrl: false` parks the
 * value in local state instead; the next call without it commits the value to
 * the URL and drops the draft.
 *
 * Values are exposed as `undefined` rather than nuqs' `null` so that builder
 * state keeps matching the shape of a `Widget`.
 */
export function useDraftableQueryState<T>(
  key: string,
  parser: UseQueryStateOptions<T>
): [T | undefined, SetDraftableValue<T>] {
  const [urlValue, setUrlValue] = useQueryState<T>(key, parser);

  // Wrapped in an object so that a draft of `undefined` is still a draft.
  const [draft, setDraft] = useState<{value: T | undefined} | null>(() =>
    // A missing param used to reach the parsers as an empty string, and several
    // of them turn that into a real value (a table display type, the errors
    // dataset). nuqs skips `parse` for an absent key, so that seed is applied
    // here — as a draft, because it only holds until the field is first set:
    // clearing the field afterwards has to leave it empty, not revive the seed.
    urlValue === null ? {value: parseAbsent(parser) ?? undefined} : null
  );

  const setValue = useCallback<SetDraftableValue<T>>(
    (value, {updateUrl = true} = {}) => {
      if (!updateUrl) {
        setDraft({value: value ?? undefined});
        return;
      }

      // An explicit `null` clears the param but is kept locally: callers rely on
      // the difference between a field that was never set and one that was
      // cleared, since only the latter should overwrite a saved widget.
      setDraft(value === null ? {value: null as T} : null);
      setUrlValue(value ?? null);
    },
    [setUrlValue]
  );

  return [draft ? draft.value : (urlValue ?? undefined), setValue];
}
