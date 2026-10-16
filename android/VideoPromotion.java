import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.Base64;
import android.view.InputDevice;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.io.File;
import java.io.FileOutputStream;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.JSONObject;

/** Small, bounded steps for the Seller Create Video Ads form. */
public final class VideoPromotion {
    private static JSONObject result(String status) throws Exception {
        return new JSONObject().put("result", status);
    }

    private static AccessibilityNodeInfo root(UiAutomation ui) throws Exception {
        // A fresh UiAutomation connection can briefly return no active root.
        // Wait for Seller instead of failing before the first button lookup.
        long deadline = SystemClock.uptimeMillis() + 2500;
        String activePackage = "尚未取得界面";
        do {
            ui.clearCache();
            AccessibilityNodeInfo root = ui.getRootInActiveWindow();
            if (root != null) {
                activePackage = String.valueOf(root.getPackageName());
                if ("com.shopee.shopeeseller".equals(activePackage)) return root;
            }
            SystemClock.sleep(120);
        } while (SystemClock.uptimeMillis() < deadline);
        throw new IllegalStateException("等待 Shopee Seller 界面未完成（当前：" + activePackage + "）");
    }

    private static boolean shareMenu(AccessibilityNodeInfo root) {
        return VideoButtons.find(root, "Copy Link") != null
                || VideoButtons.find(root, "Share with Friends and Family!") != null;
    }

    private static AccessibilityNodeInfo menuPromote(AccessibilityNodeInfo root) {
        if (!shareMenu(root)) return null;
        AccessibilityNodeInfo copy = VideoButtons.find(root, "Copy Link");
        AccessibilityNodeInfo title = VideoButtons.find(root, "Share with Friends and Family!");
        Rect screen = bounds(root);
        // The menu's orange Promote is left of Copy Link on the same row.
        // Ignore the video's identically named right-hand button behind the menu.
        for (AccessibilityNodeInfo node : nodes(root)) {
            if (!"com.shopee.shopeeseller".contentEquals(String.valueOf(node.getPackageName()))
                    || !node.isVisibleToUser() || !node.isEnabled()) continue;
            if (!"Promote".equalsIgnoreCase(text(node))
                    && !"Promote".equalsIgnoreCase(String.valueOf(node.getContentDescription()).trim())) continue;
            Rect r = bounds(node);
            if (r.isEmpty() || r.centerX() >= screen.centerX()) continue;
            if (copy != null) {
                Rect c = bounds(copy);
                if (r.centerX() < c.centerX()
                        && Math.abs(r.centerY() - c.centerY()) < Math.max(24, screen.height() * .06f)) return node;
            } else if (title != null && r.top > bounds(title).bottom) {
                return node;
            }
        }
        return null;
    }

    private static void collect(AccessibilityNodeInfo node, List<AccessibilityNodeInfo> nodes, int depth) {
        if (node == null || depth > 35 || nodes.size() >= 500) return;
        nodes.add(node);
        for (int i = 0; i < node.getChildCount(); i++) collect(node.getChild(i), nodes, depth + 1);
    }

    private static List<AccessibilityNodeInfo> nodes(AccessibilityNodeInfo root) {
        List<AccessibilityNodeInfo> nodes = new ArrayList<>();
        collect(root, nodes, 0);
        return nodes;
    }

    private static AccessibilityNodeInfo id(AccessibilityNodeInfo root, String id) {
        for (AccessibilityNodeInfo n : nodes(root)) {
            if (id.equals(n.getViewIdResourceName())) return n;
        }
        return null;
    }

    private static Rect bounds(AccessibilityNodeInfo node) {
        Rect r = new Rect();
        if (node != null) node.getBoundsInScreen(r);
        return r;
    }

    private static String text(AccessibilityNodeInfo node) {
        return node == null || node.getText() == null ? "" : node.getText().toString().trim();
    }

    private static String childText(AccessibilityNodeInfo node) {
        if (node == null) return "";
        List<String> labels = new ArrayList<>();
        for (AccessibilityNodeInfo n : nodes(node)) if (!text(n).isEmpty()) labels.add(text(n));
        return String.join(" ", labels);
    }

    private static boolean visibleOption(AccessibilityNodeInfo node) {
        Rect r = bounds(node);
        return node != null && node.isVisibleToUser() && node.isEnabled() && r.height() >= 24 && r.width() >= 30;
    }

    private static boolean form(AccessibilityNodeInfo root) {
        return VideoButtons.find(root, "Create Video Ads") != null;
    }

    private static void requireForm(AccessibilityNodeInfo root) {
        if (!form(root)) throw new IllegalStateException("没有找到 Create Video Ads 表单");
    }

    private static JSONObject scrollForm(UiAutomation ui, AccessibilityNodeInfo root, AccessibilityNodeInfo target) throws Exception {
        requireForm(root);
        AccessibilityNodeInfo scroll = null;
        for (AccessibilityNodeInfo n : nodes(root)) {
            if (String.valueOf(n.getClassName()).endsWith("ScrollView") && n.isScrollable()
                    && bounds(n).height() > bounds(root).height() / 2) { scroll = n; break; }
        }
        if (scroll == null) throw new IllegalStateException("未找到投广参数滚动区域");
        Rect r = bounds(target);
        int direction = target != null && r.bottom < r.top && r.bottom < bounds(root).height() / 2
                ? AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD : AccessibilityNodeInfo.ACTION_SCROLL_FORWARD;
        if (!scroll.performAction(direction)) throw new IllegalStateException("投广参数尚未完整显示，无法继续滚动");
        SystemClock.sleep(350);
        return result("pending");
    }

    private static boolean orange(int pixel) {
        return Color.red(pixel) > 200 && Color.green(pixel) < 225 && Color.blue(pixel) < 210
                && Color.red(pixel) - Color.green(pixel) > 20 && Color.green(pixel) - Color.blue(pixel) > 8;
    }

    private static boolean chosen(UiAutomation ui, AccessibilityNodeInfo node) {
        if (!visibleOption(node)) return false;
        if (node.isChecked() || node.isSelected()) return true;
        // Seller's React Native buttons do not expose selected/checked. The actual
        // selected option has an orange outline; unselected options have a grey fill.
        Bitmap screenshot = ui.takeScreenshot();
        if (screenshot == null) return false;
        try {
            Rect r = bounds(node);
            if (r.left < 0 || r.top < 0 || r.right > screenshot.getWidth() || r.bottom > screenshot.getHeight()) return false;
            int hits = 0, samples = 0;
            for (int x = r.left + r.width() / 4; x < r.right - r.width() / 4; x++) {
                for (int edge = 0; edge < 3; edge++) {
                    if (orange(screenshot.getPixel(x, r.top + edge))) hits++;
                    if (orange(screenshot.getPixel(x, r.bottom - 1 - edge))) hits++;
                    samples += 2;
                }
            }
            return samples > 0 && hits >= samples * 0.12;
        } finally { screenshot.recycle(); }
    }

    private static void swipe(UiAutomation ui, float x, float startY, float endY) {
        long start = SystemClock.uptimeMillis();
        for (int i = 0; i <= 16; i++) {
            int action = i == 0 ? MotionEvent.ACTION_DOWN : i == 16 ? MotionEvent.ACTION_UP : MotionEvent.ACTION_MOVE;
            MotionEvent event = MotionEvent.obtain(start, SystemClock.uptimeMillis(), action,
                    x, startY + (endY - startY) * i / 16, 0);
            event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
            try { if (!ui.injectInputEvent(event, true)) throw new IllegalStateException("天数滚轮滑动失败"); }
            finally { event.recycle(); }
            SystemClock.sleep(28);
        }
        SystemClock.sleep(400);
    }

    private static JSONObject openShareMenu(UiAutomation ui, AccessibilityNodeInfo root) throws Exception {
        if (form(root)) return result("menu_open").put("form_already_open", true);
        if (shareMenu(root)) {
            return result(menuPromote(root) != null ? "menu_open" : "pending")
                    .put("detail", "正在等待分享菜单左下方的 Promote 加载");
        }
        AccessibilityNodeInfo entry = VideoButtons.find(root, "More");
        String label = "More";
        if (entry == null) { entry = VideoButtons.findPromote(root); label = "Promote"; }
        if (entry == null) return result("pending").put("detail", "正在等待视频右侧的 More / Promote 入口");
        if (!VideoButtons.click(ui, entry)) throw new IllegalStateException("无法点击视频上的 " + label + " 入口");
        SystemClock.sleep(5000);
        // Confirm the sheet on the next step with a fresh, retryable root lookup.
        return result("pending")
                .put("entry_button", label).put("detail", "已点击视频入口并等待 5 秒，正在确认分享菜单");
    }

    private static JSONObject open(UiAutomation ui, AccessibilityNodeInfo root, JSONObject params) throws Exception {
        if (form(root)) return result("opened");
        if (params.optBoolean("menu_promote_clicked")) {
            return result("pending").put("detail", "已点击菜单内 Promote，正在等待 Create Video Ads 页面");
        }
        AccessibilityNodeInfo promote = menuPromote(root);
        if (promote == null) return result("pending").put("detail", "正在等待分享菜单内的橙色 Promote 按钮");
        if (!VideoButtons.click(ui, promote)) throw new IllegalStateException("无法点击分享菜单内的 Promote");
        SystemClock.sleep(600);
        return result("pending").put("menu_promote_clicked", true)
                .put("detail", "已点击菜单内 Promote，正在等待 Create Video Ads 页面");
    }

    private static AccessibilityNodeInfo durationOption(AccessibilityNodeInfo root, int days) {
        AccessibilityNodeInfo standard = id(root, String.valueOf(days));
        return standard != null ? standard : id(root, "customiseDuration");
    }

    private static boolean durationMatches(AccessibilityNodeInfo node, int days) {
        String value = childText(node);
        return days == 0 ? value.equalsIgnoreCase("No End Date")
                : value.matches("(?i)" + days + "\\s*days?");
    }

    private static JSONObject duration(UiAutomation ui, AccessibilityNodeInfo root, int days) throws Exception {
        requireForm(root);
        if (VideoButtons.find(root, "Customise Duration") != null) {
            AccessibilityNodeInfo selected = null, wheel = null;
            int height = bounds(root).height();
            for (AccessibilityNodeInfo n : nodes(root)) {
                Rect r = bounds(n);
                if (!String.valueOf(n.getClassName()).endsWith("ScrollView") || !n.isScrollable()) continue;
                if (r.width() > bounds(root).width() / 2 && r.height() > 25 && r.height() < height * 0.16
                        && childText(n).matches("\\d+")) selected = n;
                if (r.width() > bounds(root).width() / 2 && r.height() > height * 0.25 && r.height() < height * 0.5) wheel = n;
            }
            if (selected == null || wheel == null) throw new IllegalStateException("无法读取自定义天数滚轮当前值");
            int current = Integer.parseInt(childText(selected));
            if (current == days) {
                AccessibilityNodeInfo next = VideoButtons.find(root, "Next");
                if (next == null || !VideoButtons.click(ui, next)) throw new IllegalStateException("无法确认自定义天数");
                SystemClock.sleep(300);
            } else {
                Rect r = bounds(wheel);
                float distance = Math.min(Math.abs(days - current), 2) * bounds(selected).height();
                distance = Math.min(distance, r.height() * 0.45f) * Math.signum(days - current);
                swipe(ui, r.centerX(), r.centerY() + distance / 2, r.centerY() - distance / 2);
            }
            return result("pending").put("current_days", current);
        }
        AccessibilityNodeInfo option = durationOption(root, days);
        if (!visibleOption(option)) return scrollForm(ui, root, option);
        if (durationMatches(option, days) && chosen(ui, option)) return result("configured");
        if (!VideoButtons.click(ui, option)) throw new IllegalStateException("无法选择 Duration");
        SystemClock.sleep(250);
        return result("pending");
    }

    private static String budgetId(String budget) {
        return new BigDecimal(budget).multiply(new BigDecimal("100000")).toBigIntegerExact().toString();
    }

    private static AccessibilityNodeInfo budgetOption(AccessibilityNodeInfo root, String budget) {
        AccessibilityNodeInfo standard = id(root, budgetId(budget));
        return standard != null ? standard : id(root, "customiseBudget");
    }

    private static boolean budgetMatches(AccessibilityNodeInfo node, String budget) {
        return amountMatches(childText(node), budget);
    }

    private static boolean amountMatches(String observed, String budget) {
        try {
            String value = observed.replace('\u00a0', ' ').trim().replaceFirst("(?i)^(?:RM|MYR)\\s*", "");
            if (!value.matches("(?:\\d+|\\d{1,3}(?:,\\d{3})+)(?:\\.\\d+)?")) return false;
            return new BigDecimal(value.replace(",", "")).compareTo(new BigDecimal(budget)) == 0;
        } catch (RuntimeException error) { return false; }
    }

    private static AccessibilityNodeInfo budgetInput(AccessibilityNodeInfo root) {
        AccessibilityNodeInfo title = VideoButtons.find(root, "Customise Daily Budget");
        if (title == null) title = VideoButtons.find(root, "Customize Daily Budget");
        if (title == null) return null;
        for (AccessibilityNodeInfo n : nodes(root)) {
            if (String.valueOf(n.getClassName()).endsWith("EditText") && n.isEnabled() && n.isVisibleToUser()
                    && !bounds(n).isEmpty() && bounds(n).top >= bounds(title).bottom) return n;
        }
        return null;
    }

    private static String inputText(AccessibilityNodeInfo input) {
        if (input == null || input.isShowingHintText()) return "";
        String value = text(input);
        CharSequence hint = input.getHintText();
        return hint != null && value.equals(hint.toString().trim()) ? "" : value;
    }

    private static void typeBudget(UiAutomation ui, String value) {
        // Send the same numeric characters a user types. Bulk SET_TEXT can race
        // React Native's currency formatter; omit unnecessary decimal zeroes.
        KeyEvent[] events = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD).getEvents(value.toCharArray());
        if (events == null) throw new IllegalStateException("无法生成预算数字输入");
        for (KeyEvent event : events) {
            KeyEvent key = KeyEvent.changeTimeRepeat(event, SystemClock.uptimeMillis(), 0);
            key.setSource(InputDevice.SOURCE_KEYBOARD);
            if (!ui.injectInputEvent(key, true)) throw new IllegalStateException("预算数字输入失败");
            if (key.getAction() == KeyEvent.ACTION_UP) SystemClock.sleep(100);
        }
    }

    private static void clearBudget(UiAutomation ui, AccessibilityNodeInfo input) {
        input.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
        if (!VideoButtons.click(ui, input)) throw new IllegalStateException("无法聚焦自定义日预算输入框");
        Bundle empty = new Bundle();
        empty.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "");
        if (input.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, empty)) return;
        String current = inputText(input);
        if (current.isEmpty()) return;
        Bundle selection = new Bundle();
        selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, 0);
        selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, current.length());
        if (!input.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, selection)) {
            throw new IllegalStateException("无法清空原日预算，已停止填写");
        }
        long now = SystemClock.uptimeMillis();
        if (!ui.injectInputEvent(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_DEL, 0), true)
                || !ui.injectInputEvent(new KeyEvent(now, now + 40, KeyEvent.ACTION_UP, KeyEvent.KEYCODE_DEL, 0), true)) {
            throw new IllegalStateException("无法删除原日预算");
        }
    }

    private static JSONObject budgetPending(JSONObject state, String detail, String observed) throws Exception {
        return result("pending").put("budget_state", state).put("detail", detail).put("budget_observed", observed);
    }

    private static JSONObject budget(UiAutomation ui, AccessibilityNodeInfo root, JSONObject params) throws Exception {
        requireForm(root);
        String value = params.getString("daily_budget");
        String typedValue = new BigDecimal(value).stripTrailingZeros().toPlainString();
        JSONObject state = params.optJSONObject("budget_state");
        if (state == null) state = new JSONObject();
        String phase = state.optString("phase");
        if (VideoButtons.find(root, "Customise Daily Budget") != null
                || VideoButtons.find(root, "Customize Daily Budget") != null) {
            AccessibilityNodeInfo input = budgetInput(root);
            if (input == null) return budgetPending(state, "正在等待自定义日预算输入框加载", "");
            String observed = inputText(input);
            String comparison = "预期 RM" + value + "，输入框实际为「" + observed + "」";
            if ("confirming".equals(phase)) return budgetPending(state, "已点击 Next，正在等待预算选择器关闭；" + comparison, observed);
            if (phase.isEmpty()) {
                clearBudget(ui, input);
                state.put("phase", "clearing").put("attempts", 1).put("checks", 0).put("stable_reads", 0);
                return budgetPending(state, "正在清空原日预算，准备输入 RM" + value, observed);
            }
            if ("clearing".equals(phase)) {
                if (!observed.isEmpty()) {
                    int checks = state.optInt("checks") + 1;
                    state.put("checks", checks);
                    if (checks >= 3) throw new IllegalStateException("清空原日预算未完成；" + comparison);
                    return budgetPending(state, "正在等待原日预算清空；" + comparison, observed);
                }
                if (!input.isFocused()) {
                    if (!input.performAction(AccessibilityNodeInfo.ACTION_FOCUS)) {
                        throw new IllegalStateException("自定义日预算输入框未获得焦点");
                    }
                    return budgetPending(state, "正在确认自定义日预算输入框已获得焦点", observed);
                }
                typeBudget(ui, typedValue);
                state.put("phase", "reading").put("checks", 0).put("stable_reads", 0);
                return budgetPending(state, "已逐字输入 " + typedValue + "，正在等待日预算更新", observed);
            }
            if (amountMatches(observed, value)) {
                int stable = state.optInt("stable_reads") + 1;
                state.put("stable_reads", stable).put("checks", 0);
                if (stable < 2) return budgetPending(state, "金额已匹配，正在再次确认稳定值；" + comparison, observed);
                AccessibilityNodeInfo next = VideoButtons.find(root, "Next");
                if (next == null) return budgetPending(state, "金额已确认，正在等待 Next 按钮", observed);
                if (!VideoButtons.click(ui, next)) throw new IllegalStateException("无法点击自定义日预算的 Next");
                state.put("phase", "confirming");
                return budgetPending(state, "日预算已确认，已点击 Next，正在核对表单金额", observed);
            }
            int checks = state.optInt("checks") + 1;
            state.put("checks", checks).put("stable_reads", 0);
            if (checks >= 3) {
                if (state.optInt("attempts") >= 2) throw new IllegalStateException("日预算输入重试后仍不一致；" + comparison);
                clearBudget(ui, input);
                state.put("phase", "clearing").put("attempts", 2).put("checks", 0);
                return budgetPending(state, "金额尚未匹配，正在清空并重试一次；" + comparison, observed);
            }
            return budgetPending(state, "正在等待日预算金额更新；" + comparison, observed);
        }
        AccessibilityNodeInfo option = budgetOption(root, value);
        if (!visibleOption(option)) return scrollForm(ui, root, option);
        if (budgetMatches(option, value) && chosen(ui, option)) {
            return result("configured").put("budget_observed", childText(option));
        }
        if ("confirming".equals(phase)) return budgetPending(state,
                "正在确认表单已选中日预算 RM" + value + "，表单实际为「" + childText(option) + "」", childText(option));
        if (!VideoButtons.click(ui, option)) throw new IllegalStateException("无法选择 Daily Budget");
        SystemClock.sleep(250);
        return result("pending");
    }

    private static JSONObject objective(UiAutomation ui, AccessibilityNodeInfo root, String objective) throws Exception {
        requireForm(root);
        AccessibilityNodeInfo option = id(root, objective.equals("GMV Max") ? "max_gmv" : "max_view");
        if (!visibleOption(option)) return scrollForm(ui, root, option);
        if (childText(option).equalsIgnoreCase(objective) && chosen(ui, option)) return result("configured");
        if (!VideoButtons.click(ui, option)) throw new IllegalStateException("无法选择 Objective");
        SystemClock.sleep(250);
        return result("pending");
    }

    private static JSONObject verify(UiAutomation ui, AccessibilityNodeInfo root, JSONObject params) throws Exception {
        requireForm(root);
        if (VideoButtons.find(root, "Next") != null) throw new IllegalStateException("仍有参数选择器未关闭");
        AccessibilityNodeInfo d = durationOption(root, params.getInt("days"));
        AccessibilityNodeInfo b = budgetOption(root, params.getString("daily_budget"));
        AccessibilityNodeInfo o = id(root, params.getString("objective").equals("GMV Max") ? "max_gmv" : "max_view");
        if (!durationMatches(d, params.getInt("days")) || !chosen(ui, d)) throw new IllegalStateException("Duration 尚未确认选中 Excel 指定值");
        if (!budgetMatches(b, params.getString("daily_budget")) || !chosen(ui, b)) throw new IllegalStateException("Daily Budget 尚未确认选中 Excel 指定值");
        if (!childText(o).equalsIgnoreCase(params.getString("objective")) || !chosen(ui, o)) throw new IllegalStateException("Objective 尚未确认选中 Excel 指定值");
        return result("verified").put("duration", childText(d)).put("daily_budget", childText(b)).put("objective", childText(o));
    }

    private static Set<String> visibleTexts(AccessibilityNodeInfo root) {
        Set<String> text = new LinkedHashSet<>();
        for (AccessibilityNodeInfo n : nodes(root)) {
            if (n.isVisibleToUser() && !bounds(n).isEmpty() && !text(n).isEmpty()) text.add(text(n));
        }
        return text;
    }

    private static void captureFrame(UiAutomation ui, File directory, String name) throws Exception {
        Bitmap frame = ui.takeScreenshot();
        if (frame == null) throw new IllegalStateException("无法取得发布结果截图");
        try (FileOutputStream output = new FileOutputStream(new File(directory, name))) {
            if (!frame.compress(Bitmap.CompressFormat.PNG, 100, output)) throw new IllegalStateException("保存发布结果截图失败");
        } finally { frame.recycle(); }
    }

    private static void collectNewText(AccessibilityNodeInfo root, Set<String> before, Set<String> messages) {
        if (root == null || !"com.shopee.shopeeseller".contentEquals(String.valueOf(root.getPackageName()))) return;
        for (String value : visibleTexts(root)) if (!before.contains(value)) messages.add(value);
    }

    private static JSONObject publish(UiAutomation ui, AccessibilityNodeInfo root, JSONObject params) throws Exception {
        // Recheck immediately before the only Publish click. Never retry this action.
        try { verify(ui, root, params); }
        catch (Exception error) { return result("not_published").put("error", error.getMessage()); }
        AccessibilityNodeInfo publish = VideoButtons.find(root, "Publish");
        if (publish == null) return result("not_published").put("error", "找不到 Publish 按钮");
        Rect r = bounds(publish);
        if (r.isEmpty()) return result("not_published").put("error", "Publish 按钮不可见");
        String captureId = params.getString("capture_id");
        if (!captureId.matches("[0-9a-f]{32}")) return result("not_published").put("error", "发布截图编号无效");
        File directory = new File("/data/local/tmp/videoqa-popup-" + captureId);
        List<String> captureErrors = Collections.synchronizedList(new ArrayList<>());
        if (!directory.isDirectory() && !directory.mkdirs()) return result("not_published").put("error", "无法准备发布结果截图目录");
        try { captureFrame(ui, directory, "before.png"); }
        catch (Exception error) { return result("not_published").put("error", "无法准备发布前截图：" + error.getMessage()); }
        Set<String> before = visibleTexts(root);
        Set<String> messages = Collections.synchronizedSet(new LinkedHashSet<>());
        AtomicBoolean attempted = new AtomicBoolean(false);
        AtomicBoolean sampling = new AtomicBoolean(true);
        AccessibilityServiceInfo info = ui.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
        info.eventTypes |= AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED | AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                | AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED | AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED;
        ui.setServiceInfo(info);
        ui.setOnAccessibilityEventListener(event -> {
            if (attempted.get() && "com.shopee.shopeeseller".contentEquals(String.valueOf(event.getPackageName()))) {
                for (CharSequence value : event.getText()) {
                    if (value != null && value.length() > 0 && !before.contains(value.toString().trim())) messages.add(value.toString().trim());
                }
                CharSequence description = event.getContentDescription();
                if (description != null && description.length() > 0 && !before.contains(description.toString().trim())) messages.add(description.toString().trim());
            }
        });
        Thread sampler = new Thread(() -> {
            long started = SystemClock.uptimeMillis();
            for (int delay : new int[]{100, 350, 650, 1000, 1500, 2200, 3000, 4000, 5500, 7000, 8500}) {
                while (sampling.get() && SystemClock.uptimeMillis() < started + delay) SystemClock.sleep(40);
                if (!sampling.get()) return;
                try { captureFrame(ui, directory, String.format(java.util.Locale.ROOT, "frame-%05d.png", delay)); }
                catch (Exception error) { captureErrors.add(error.toString()); }
            }
        }, "publish-popup-frames");
        sampler.setDaemon(true);
        JSONObject response;
        try {
            // Use a single input gesture; an accessibility click returning false could
            // still have been delivered, so do not issue a second fallback click.
            long t = SystemClock.uptimeMillis();
            MotionEvent down = MotionEvent.obtain(t, t, MotionEvent.ACTION_DOWN, r.centerX(), r.centerY(), 0);
            MotionEvent up = MotionEvent.obtain(t, t + 50, MotionEvent.ACTION_UP, r.centerX(), r.centerY(), 0);
            down.setSource(InputDevice.SOURCE_TOUCHSCREEN); up.setSource(InputDevice.SOURCE_TOUCHSCREEN);
            attempted.set(true);
            try { ui.injectInputEvent(down, true); ui.injectInputEvent(up, true); }
            finally { down.recycle(); up.recycle(); }
            sampler.start();
            long deadline = SystemClock.uptimeMillis() + 9000;
            while (SystemClock.uptimeMillis() < deadline) {
                try {
                    ui.clearCache();
                    collectNewText(ui.getRootInActiveWindow(), before, messages);
                    for (AccessibilityWindowInfo window : ui.getWindows()) collectNewText(window.getRoot(), before, messages);
                } catch (Exception error) {
                    // A transient/missing accessibility window must not stop the
                    // independent frame sampler before the result toast appears.
                    String detail = "控件文字读取：" + error.toString();
                    if (!captureErrors.contains(detail)) captureErrors.add(detail);
                }
                SystemClock.sleep(150);
            }
            response = result("publish_attempted");
        } catch (Exception error) {
            response = result(attempted.get() ? "publish_unknown" : "not_published").put("error", error.toString());
        } finally {
            sampling.set(false);
            sampler.join(1500);
            ui.setOnAccessibilityEventListener(null);
        }
        synchronized (messages) { response.put("popup_text", String.join("\n", messages)); }
        synchronized (captureErrors) { if (!captureErrors.isEmpty()) response.put("popup_capture_error", String.join("\n", captureErrors)); }
        return response;
    }

    private static JSONObject back(UiAutomation ui, AccessibilityNodeInfo root) throws Exception {
        if (!form(root) && VideoButtons.find(root, "Copy Link") == null && VideoButtons.isVideoPlayer(root)) return result("returned");
        if (!VideoButtons.videoCards(root).isEmpty() && !form(root)) return result("grid");
        if (!ui.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)) throw new IllegalStateException("无法返回视频页面");
        SystemClock.sleep(500);
        return result("pending");
    }

    public static JSONObject run(UiAutomation ui, String mode) throws Exception {
        String[] parts = mode.split(":", 2);
        JSONObject params = parts.length == 2 ? new JSONObject(new String(Base64.decode(parts[1], Base64.DEFAULT), StandardCharsets.UTF_8)) : new JSONObject();
        AccessibilityNodeInfo root;
        try {
            root = root(ui);
        } catch (IllegalStateException error) {
            // Pre-Publish steps may retry while the UI connection or activity settles.
            // Publish itself must never be retried after an ambiguous click.
            return result("ad-publish".equals(parts[0]) ? "not_published" : "pending")
                    .put("error", error.getMessage());
        }
        switch (parts[0]) {
            case "ad-share-menu": return openShareMenu(ui, root);
            case "ad-open": return open(ui, root, params);
            case "ad-duration": return duration(ui, root, params.getInt("days"));
            case "ad-budget": return budget(ui, root, params);
            case "ad-objective": return objective(ui, root, params.getString("objective"));
            case "ad-verify": return verify(ui, root, params);
            case "ad-publish": return publish(ui, root, params);
            case "ad-return": return back(ui, root);
            default: throw new IllegalArgumentException("Unknown promotion action");
        }
    }
}
