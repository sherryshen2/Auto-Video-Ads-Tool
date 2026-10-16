import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityService;
import android.graphics.Rect;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import java.lang.reflect.Method;
import java.util.List;
import java.util.ArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.json.JSONObject;

/** ADB-only helper: locate the two share controls without waiting for video playback to idle. */
public final class VideoButtons {
    private static final String SELLER = "com.shopee.shopeeseller";
    private static final long MORE_TO_COPY_DELAY_MS = 5000;
    private static final Pattern VIDEO_CARD = Pattern.compile("click video (\\d+)");

    private static int cardIndex(AccessibilityNodeInfo node) {
        Matcher matcher = VIDEO_CARD.matcher(String.valueOf(node.getContentDescription()).trim());
        return matcher.matches() ? Integer.parseInt(matcher.group(1)) : -1;
    }

    static List<AccessibilityNodeInfo> videoCards(AccessibilityNodeInfo root) {
        List<AccessibilityNodeInfo> cards = new ArrayList<>();
        for (AccessibilityNodeInfo node : root.findAccessibilityNodeInfosByText("click video")) {
            if (SELLER.contentEquals(String.valueOf(node.getPackageName())) && node.isVisibleToUser()
                    && node.isEnabled() && cardIndex(node) >= 0) cards.add(node);
        }
        cards.sort((a, b) -> Integer.compare(cardIndex(a), cardIndex(b)));
        return cards;
    }

    private static AccessibilityNodeInfo gridScroll(AccessibilityNodeInfo card) {
        // The Seller profile has a horizontal ViewPager around its vertical ScrollView.
        for (AccessibilityNodeInfo node = card; node != null; node = node.getParent()) {
            String type = String.valueOf(node.getClassName());
            if (type.contains("ScrollView") || type.contains("RecyclerView") || type.contains("ListView")) return node;
        }
        return null;
    }

    private static boolean hasLoadingIndicator(AccessibilityNodeInfo node, int depth) {
        if (node == null || depth > 30 || !node.isVisibleToUser()) return false;
        String type = String.valueOf(node.getClassName());
        String text = String.valueOf(node.getText()).trim();
        if (type.endsWith("ProgressBar") || text.matches("(?i)loading[.\\u2026]*") || text.contains("加载中")) return true;
        for (int i = 0; i < node.getChildCount(); i++) {
            if (hasLoadingIndicator(node.getChild(i), depth + 1)) return true;
        }
        return false;
    }

    private static String gridSignature(List<AccessibilityNodeInfo> cards) {
        StringBuilder signature = new StringBuilder();
        for (AccessibilityNodeInfo card : cards) {
            Rect bounds = new Rect();
            card.getBoundsInScreen(bounds);
            signature.append(cardIndex(card)).append(':').append(bounds.toShortString()).append(';');
        }
        return signature.toString();
    }

    private static JSONObject openGridVideo(UiAutomation ui, int wanted) throws Exception {
        JSONObject result = new JSONObject().put("requested_index", wanted);
        long deadline = SystemClock.uptimeMillis() + 12000;
        int backs = 0;
        long nextBackAt = 0;
        String previous = "";
        long bottomSince = 0;
        boolean bottomRejected = false;
        while (SystemClock.uptimeMillis() < deadline) {
            ui.clearCache();
            AccessibilityNodeInfo root = ui.getRootInActiveWindow();
            if (root == null) { SystemClock.sleep(150); continue; }
            String app = String.valueOf(root.getPackageName());
            if ("local.videoqa.clipboard".equals(app)) { SystemClock.sleep(150); continue; }
            if (!SELLER.equals(app)) return result.put("result", "wrong_app");
            List<AccessibilityNodeInfo> cards = videoCards(root);
            if (cards.isEmpty() || isVideoPlayer(root) || find(root, "Copy Link") != null) {
                if (SystemClock.uptimeMillis() < nextBackAt || (cards.isEmpty() && hasLoadingIndicator(root, 0))) {
                    SystemClock.sleep(150);
                    continue;
                }
                if (backs >= 3) return result.put("result", "grid_missing");
                if (!ui.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)) return result.put("result", "back_failed");
                backs++;
                nextBackAt = SystemClock.uptimeMillis() + 1800;
                SystemClock.sleep(350);
                continue;
            }
            for (AccessibilityNodeInfo card : cards) {
                if (cardIndex(card) == wanted) {
                    return result.put("result", click(ui, card) ? "opened" : "click_failed").put("video_index", wanted);
                }
            }
            int first = cardIndex(cards.get(0));
            int last = cardIndex(cards.get(cards.size() - 1));
            result.put("last_visible_index", last);
            if (wanted >= first && wanted <= last) return result.put("result", "card_missing");
            AccessibilityNodeInfo scroll = gridScroll(cards.get(0));
            if (scroll == null) return result.put("result", "scroll_missing");
            boolean forward = wanted > last;
            String signature = gridSignature(cards);
            boolean loading = hasLoadingIndicator(root, 0);
            int action = forward ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD;
            boolean supportsScroll = false;
            for (AccessibilityNodeInfo.AccessibilityAction supported : scroll.getActionList()) {
                if (supported.getId() == action) supportsScroll = true;
            }
            boolean moved = supportsScroll && scroll.performAction(action);
            // Only a stable bottom with a rejected native forward-scroll counts as list exhaustion.
            // A successful scroll with unchanged cards is a navigation problem, never proof of completion.
            if (forward && !moved && !loading && signature.equals(previous)) {
                if (!bottomRejected) bottomSince = SystemClock.uptimeMillis();
                bottomRejected = true;
                if (SystemClock.uptimeMillis() - bottomSince >= 4000 && last == wanted - 1) {
                    return result.put("result", "list_end").put("video_total", wanted);
                }
            } else {
                bottomRejected = false;
            }
            previous = signature;
            SystemClock.sleep(650);
        }
        return result.put("result", "loading_or_stalled");
    }

    static AccessibilityNodeInfo find(AccessibilityNodeInfo root, String label) {
        List<AccessibilityNodeInfo> nodes = root.findAccessibilityNodeInfosByText(label);
        for (AccessibilityNodeInfo node : nodes) {
            if (!SELLER.contentEquals(String.valueOf(node.getPackageName()))
                    || !node.isVisibleToUser() || !node.isEnabled()) continue;
            String text = String.valueOf(node.getText()).trim();
            String description = String.valueOf(node.getContentDescription()).trim();
            if (label.equalsIgnoreCase(text) || label.equalsIgnoreCase(description)) {
                if (!"More".equals(label) || isVideoShareControl(node)) return node;
            }
        }
        return null;
    }

    private static boolean isVideoShareControl(AccessibilityNodeInfo node) {
        for (int depth = 0; node != null && depth < 5; depth++, node = node.getParent()) {
            if (String.valueOf(node.getContentDescription()).toLowerCase(java.util.Locale.ROOT)
                    .contains("video share icon")) return true;
        }
        return false;
    }

    static AccessibilityNodeInfo findPromote(AccessibilityNodeInfo root) {
        // Promote is accepted only in the video's right-hand action column.
        Rect screen = new Rect();
        root.getBoundsInScreen(screen);
        if (screen.isEmpty()) return null;
        for (AccessibilityNodeInfo node : root.findAccessibilityNodeInfosByText("Promote")) {
            if (!SELLER.contentEquals(String.valueOf(node.getPackageName()))
                    || !node.isVisibleToUser() || !node.isEnabled()) continue;
            String text = String.valueOf(node.getText()).trim();
            String description = String.valueOf(node.getContentDescription()).trim();
            if (!"Promote".equalsIgnoreCase(text) && !"Promote".equalsIgnoreCase(description)) continue;
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            if (bounds.isEmpty()) continue;
            float x = (bounds.centerX() - screen.left) / (float) screen.width();
            float y = (bounds.centerY() - screen.top) / (float) screen.height();
            if (x >= 0.70f && x <= 1.0f && y >= 0.45f && y <= 0.90f) return node;
        }
        return null;
    }

    static boolean isVideoPlayer(AccessibilityNodeInfo root) {
        if (find(root, "More") != null || findPromote(root) != null) return true;
        for (AccessibilityNodeInfo node : root.findAccessibilityNodeInfosByText("video")) {
            String description = String.valueOf(node.getContentDescription()).toLowerCase(java.util.Locale.ROOT);
            if (node.isVisibleToUser() && (description.contains("video share icon")
                    || description.contains("video like icon") || description.contains("video comment icon"))) return true;
        }
        return false;
    }

    private static JSONObject dismissMenu(UiAutomation ui) throws Exception {
        long deadline = SystemClock.uptimeMillis() + 3000;
        boolean backed = false;
        while (SystemClock.uptimeMillis() < deadline) {
            ui.clearCache();
            AccessibilityNodeInfo root = ui.getRootInActiveWindow();
            if (root == null) { SystemClock.sleep(120); continue; }
            String app = String.valueOf(root.getPackageName());
            if ("local.videoqa.clipboard".equals(app)) { SystemClock.sleep(120); continue; }
            if (!SELLER.equals(app)) return new JSONObject().put("result", "wrong_app");
            boolean menuOpen = find(root, "Copy Link") != null;
            if (!menuOpen && isVideoPlayer(root)) return new JSONObject().put("result", "ready");
            if (!menuOpen && !videoCards(root).isEmpty()) return new JSONObject().put("result", "grid");
            if (!backed) {
                if (!ui.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)) {
                    return new JSONObject().put("result", "unavailable");
                }
                backed = true;
            }
            SystemClock.sleep(200);
        }
        return new JSONObject().put("result", backed ? "dismissed" : "unavailable");
    }

    private static AccessibilityNodeInfo homeTab(AccessibilityNodeInfo root) {
        Rect screen = new Rect();
        root.getBoundsInScreen(screen);
        if (screen.isEmpty()) return null;
        // Only the Seller bottom navigation tab can end the back navigation.
        for (String label : new String[]{"Home", "首页"}) {
            for (AccessibilityNodeInfo home : root.findAccessibilityNodeInfosByText(label)) {
                if (!SELLER.contentEquals(String.valueOf(home.getPackageName()))
                        || !home.isVisibleToUser() || !home.isEnabled()) continue;
                if (!label.equalsIgnoreCase(String.valueOf(home.getText()).trim())
                        && !label.equalsIgnoreCase(String.valueOf(home.getContentDescription()).trim())) continue;
                Rect tab = new Rect();
                home.getBoundsInScreen(tab);
                if (!tab.isEmpty() && tab.centerY() >= screen.top + screen.height() * .8f) return home;
            }
        }
        return null;
    }

    private static JSONObject returnHome(UiAutomation ui) throws Exception {
        long deadline = SystemClock.uptimeMillis() + 12000;
        long nextActionAt = 0;
        boolean homeClicked = false;
        int backs = 0;
        String activePackage = "";
        while (SystemClock.uptimeMillis() < deadline) {
            ui.clearCache();
            AccessibilityNodeInfo root = ui.getRootInActiveWindow();
            if (root == null) { SystemClock.sleep(120); continue; }
            activePackage = String.valueOf(root.getPackageName());
            if (!SELLER.equals(activePackage)) {
                // Allow the clipboard helper/activity transition to settle, but
                // never press Back in the launcher or another application.
                SystemClock.sleep(150);
                continue;
            }
            if (SystemClock.uptimeMillis() < nextActionAt) { SystemClock.sleep(120); continue; }
            AccessibilityNodeInfo home = homeTab(root);
            boolean videoPage = isVideoPlayer(root) || !videoCards(root).isEmpty()
                    || find(root, "Copy Link") != null || find(root, "Create Video Ads") != null;
            if (home != null && !videoPage) {
                if (homeClicked) return new JSONObject().put("result", "home").put("back_count", backs);
                if (!click(ui, home)) return new JSONObject().put("result", "home_click_failed");
                homeClicked = true;
                nextActionAt = SystemClock.uptimeMillis() + 1200;
                continue;
            }
            homeClicked = false;
            if (backs >= 6) return new JSONObject().put("result", "home_missing").put("back_count", backs);
            if (!ui.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)) {
                return new JSONObject().put("result", "back_failed");
            }
            backs++;
            nextActionAt = SystemClock.uptimeMillis() + 1200;
        }
        return new JSONObject().put("result", "home_unconfirmed").put("active_package", activePackage).put("back_count", backs);
    }

    static boolean click(UiAutomation ui, AccessibilityNodeInfo node) {
        AccessibilityNodeInfo target = node;
        for (int depth = 0; target != null && depth < 5; depth++) {
            if (!SELLER.contentEquals(String.valueOf(target.getPackageName()))) break;
            if (target.isClickable() && target.isEnabled()) {
                if (target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true;
                break;
            }
            target = target.getParent();
        }
        // Some React Native labels expose bounds but no accessibility click action.
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        if (bounds.isEmpty()) return false;
        long now = SystemClock.uptimeMillis();
        MotionEvent down = MotionEvent.obtain(now, now, MotionEvent.ACTION_DOWN,
                bounds.centerX(), bounds.centerY(), 0);
        MotionEvent up = MotionEvent.obtain(now, now + 40, MotionEvent.ACTION_UP,
                bounds.centerX(), bounds.centerY(), 0);
        down.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        up.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        try {
            boolean pressed = ui.injectInputEvent(down, true);
            boolean released = ui.injectInputEvent(up, true);
            return pressed && released;
        } finally {
            down.recycle();
            up.recycle();
        }
    }

    private static JSONObject operate(UiAutomation ui, String mode, long started) throws Exception {
        JSONObject result = new JSONObject();
        long deadline = SystemClock.uptimeMillis() + 3500;
        boolean clickedEntry = false;
        boolean sawSeller = false;
        while (SystemClock.uptimeMillis() < deadline) {
            ui.clearCache();
            AccessibilityNodeInfo root = ui.getRootInActiveWindow();
            if (root != null && !SELLER.contentEquals(String.valueOf(root.getPackageName()))) {
                return result.put("result", "wrong_app")
                        .put("active_package", String.valueOf(root.getPackageName()));
            }
            if (root != null && SELLER.contentEquals(String.valueOf(root.getPackageName()))) {
                sawSeller = true;
                AccessibilityNodeInfo copy = find(root, "Copy Link");
                if (copy != null) {
                    if ("probe".equals(mode) || "more".equals(mode)) {
                        return result.put("result", "menu_open");
                    }
                    result.put("copy_started_at", System.currentTimeMillis());
                    boolean copied = click(ui, copy);
                    return result.put("result", copied ? "copied" : "click_failed")
                            .put("copy_ms", SystemClock.uptimeMillis() - started);
                }
                if (!clickedEntry) {
                    AccessibilityNodeInfo entry = find(root, "More");
                    String label = "More";
                    if (entry == null) {
                        entry = findPromote(root);
                        label = "Promote";
                    }
                    if (entry != null) {
                        if ("probe".equals(mode)) return result.put("result", label.toLowerCase(java.util.Locale.ROOT) + "_visible");
                        if (!click(ui, entry)) return result.put("result", "click_failed");
                        clickedEntry = true;
                        result.put("entry_button", label).put("entry_ms", SystemClock.uptimeMillis() - started);
                        // Allow the share sheet to load before querying or clicking Copy Link.
                        SystemClock.sleep(MORE_TO_COPY_DELAY_MS);
                        deadline = SystemClock.uptimeMillis() + 2500;
                    }
                }
            }
            SystemClock.sleep(120);
        }
        return result.put("result", !sawSeller ? "wrong_app" :
                (clickedEntry ? "copy_missing" : "more_missing"));
    }

    public static void main(String[] args) {
        long started = SystemClock.uptimeMillis();
        String mode = args.length == 0 ? "copy-link" : args[0];
        // Accessibility Binder calls can stall while a video is playing. End our
        // own process before the ADB timeout so it cannot block subsequent swipes.
        Thread watchdog = new Thread(() -> {
            SystemClock.sleep("dismiss-menu".equals(mode) ? 5500 : mode.startsWith("ad-publish:") ? 25000 : 16000);
            System.out.println("{\"result\":\"timeout\",\"error\":\"Video UI lookup timed out\"}");
            System.out.flush();
            System.exit(0);
        });
        watchdog.setDaemon(true);
        watchdog.start();
        Object wrapper = null;
        Method disconnect = null;
        JSONObject result = new JSONObject();
        try {
            Class<?> wrapperClass = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
            wrapper = wrapperClass.getDeclaredConstructor().newInstance();
            disconnect = wrapperClass.getMethod("disconnect");
            wrapperClass.getMethod("connect").invoke(wrapper);
            UiAutomation ui = (UiAutomation) wrapperClass.getMethod("getUiAutomation").invoke(wrapper);
            if (mode.startsWith("ad-")) {
                result = VideoPromotion.run(ui, mode);
            } else if (mode.startsWith("grid-next:")) {
                result = openGridVideo(ui, Integer.parseInt(mode.substring("grid-next:".length())));
            } else if ("dismiss-menu".equals(mode)) {
                result = dismissMenu(ui);
            } else if ("return-home".equals(mode)) {
                result = returnHome(ui);
            } else {
                result = operate(ui, mode, started);
            }
        } catch (Throwable error) {
            try {
                Throwable detail = error.getCause() == null ? error : error.getCause();
                result.put("result", "error").put("error", detail.toString());
            } catch (Exception ignored) { }
        } finally {
            if (wrapper != null && disconnect != null) {
                try { disconnect.invoke(wrapper); } catch (Throwable ignored) { }
            }
        }
        try { result.put("elapsed_ms", SystemClock.uptimeMillis() - started); } catch (Exception ignored) { }
        System.out.println(result.toString());
        System.out.flush();
        System.exit(0);
    }
}
