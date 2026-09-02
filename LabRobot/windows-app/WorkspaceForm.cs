using System.Diagnostics;
using System.Drawing.Drawing2D;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace LabRobot.WindowsApp;

internal sealed class WorkspaceForm : Form
{
    private static readonly Color Shell = Color.FromArgb(24, 24, 24);
    private static readonly Color Chrome = Color.FromArgb(31, 31, 31);
    private static readonly Color Rail = Color.FromArgb(8, 8, 8);
    private static readonly Color Muted = Color.FromArgb(165, 165, 165);
    private static readonly Color Border = Color.FromArgb(54, 54, 54);

    private readonly WorkspaceOptions _options;
    private readonly WebView2 _webView = new() { Dock = DockStyle.Fill, DefaultBackgroundColor = Shell };
    private readonly TextBox _address = new();
    private readonly Button _backButton;
    private readonly Button _forwardButton;
    private readonly Dictionary<string, Button> _navigationButtons = new(StringComparer.OrdinalIgnoreCase);
    private string _activeTarget = "home";

    public WorkspaceForm(WorkspaceOptions options)
    {
        _options = options;
        Text = "Strat-Aqorynth — Lab Robot Windows App";
        BackColor = Shell;
        ForeColor = Color.White;
        Font = new Font("Segoe UI", 9F);
        MinimumSize = new Size(960, 640);
        ClientSize = new Size(1320, 820);
        StartPosition = FormStartPosition.CenterScreen;

        var topBar = BuildTopBar(out _backButton, out _forwardButton);
        var rail = BuildNavigationRail();
        var content = new Panel { Dock = DockStyle.Fill, BackColor = Shell };
        content.Controls.Add(_webView);

        Controls.Add(content);
        Controls.Add(rail);
        Controls.Add(topBar);

        Shown += async (_, _) => await InitializeWebViewAsync();
        FormClosed += (_, _) => _webView.Dispose();
    }

    private Panel BuildTopBar(out Button backButton, out Button forwardButton)
    {
        var bar = new Panel { Dock = DockStyle.Top, Height = 72, BackColor = Chrome };
        var brand = new Panel { Dock = DockStyle.Left, Width = 292, BackColor = Color.FromArgb(18, 18, 18) };
        var logo = new Label
        {
            Dock = DockStyle.Left,
            Width = 76,
            BackColor = Color.FromArgb(0, 120, 212),
            ForeColor = Color.White,
            Text = "LR",
            TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font("Segoe UI", 14F, FontStyle.Bold),
        };
        var title = new Label
        {
            Dock = DockStyle.Fill,
            Text = "Lab Robot Windows App",
            TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font("Segoe UI Semibold", 11F, FontStyle.Bold),
            ForeColor = Color.White,
        };
        brand.Controls.Add(title);
        brand.Controls.Add(logo);

        var controls = new FlowLayoutPanel
        {
            Dock = DockStyle.Left,
            Width = 202,
            Padding = new Padding(8, 15, 0, 0),
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = false,
            BackColor = Chrome,
        };
        backButton = ToolbarButton("‹", "Back", (_, _) => { if (_webView.CanGoBack) _webView.GoBack(); });
        forwardButton = ToolbarButton("›", "Forward", (_, _) => { if (_webView.CanGoForward) _webView.GoForward(); });
        controls.Controls.Add(backButton);
        controls.Controls.Add(forwardButton);
        controls.Controls.Add(ToolbarButton("↻", "Refresh", (_, _) => _webView.Reload()));
        controls.Controls.Add(ToolbarButton("⌂", "App home", (_, _) => Navigate("home")));

        var account = new Panel { Dock = DockStyle.Right, Width = 116, BackColor = Chrome };
        var avatar = new RoundLabel
        {
            Text = "LR",
            Size = new Size(38, 38),
            Location = new Point(64, 17),
            TextAlign = ContentAlignment.MiddleCenter,
            ForeColor = Color.White,
            BackColor = Color.FromArgb(28, 28, 28),
            BorderColor = Color.FromArgb(110, 110, 110),
        };
        var external = ToolbarButton("↗", "Open current page in browser", (_, _) => OpenExternally());
        external.Location = new Point(7, 15);
        account.Controls.Add(external);
        account.Controls.Add(avatar);

        _address.Dock = DockStyle.Fill;
        _address.Margin = new Padding(0);
        _address.BackColor = Color.FromArgb(43, 43, 43);
        _address.ForeColor = Color.FromArgb(220, 220, 220);
        _address.BorderStyle = BorderStyle.FixedSingle;
        _address.Font = new Font("Segoe UI", 10F);
        _address.Text = "App home";
        _address.ReadOnly = true;
        _address.TabStop = false;
        var addressHost = new Panel { Dock = DockStyle.Fill, Padding = new Padding(8, 20, 8, 18), BackColor = Chrome };
        addressHost.Controls.Add(_address);

        bar.Controls.Add(addressHost);
        bar.Controls.Add(account);
        bar.Controls.Add(controls);
        bar.Controls.Add(brand);
        return bar;
    }

    private Panel BuildNavigationRail()
    {
        var rail = new FlowLayoutPanel
        {
            Dock = DockStyle.Left,
            Width = 84,
            BackColor = Rail,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
            Padding = new Padding(0, 14, 0, 0),
        };

        AddNavigationButton(rail, "home", "⌂", "Home");
        AddNavigationButton(rail, "lab", "▰", "Lab Robot");
        AddNavigationButton(rail, "claude", "AI", "Claude");
        AddNavigationButton(rail, "veo", "▣", "Veo");
        AddNavigationButton(rail, "copilot", "{ }", "Copilot");
        return rail;
    }

    private void AddNavigationButton(Control rail, string key, string icon, string label)
    {
        var button = new Button
        {
            Width = 84,
            Height = 66,
            Margin = Padding.Empty,
            FlatStyle = FlatStyle.Flat,
            FlatAppearance = { BorderSize = 0 },
            BackColor = Rail,
            ForeColor = Color.FromArgb(210, 210, 210),
            Text = $"{icon}\n{label}",
            TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font("Segoe UI Semibold", 8.5F),
            Cursor = Cursors.Hand,
            Tag = key,
        };
        button.Click += (_, _) => Navigate(key);
        rail.Controls.Add(button);
        _navigationButtons[key] = button;
    }

    private static Button ToolbarButton(string text, string tooltip, EventHandler click)
    {
        var button = new Button
        {
            Text = text,
            Width = 42,
            Height = 40,
            Margin = new Padding(1),
            FlatStyle = FlatStyle.Flat,
            FlatAppearance = { BorderSize = 0, MouseOverBackColor = Color.FromArgb(55, 55, 55) },
            BackColor = Chrome,
            ForeColor = Color.FromArgb(190, 190, 190),
            Font = new Font("Segoe UI Symbol", 16F),
            Cursor = Cursors.Hand,
            AccessibleName = tooltip,
        };
        button.Click += click;
        new ToolTip().SetToolTip(button, tooltip);
        return button;
    }

    private async Task InitializeWebViewAsync()
    {
        try
        {
            var userData = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "StratAqorynth", "LabRobot", "WebView2");
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: userData);
            await _webView.EnsureCoreWebView2Async(environment);
            _webView.CoreWebView2.Settings.AreDevToolsEnabled = false;
            _webView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = true;
            _webView.CoreWebView2.Settings.IsStatusBarEnabled = false;
            _webView.CoreWebView2.Settings.IsZoomControlEnabled = true;
            _webView.CoreWebView2.WebMessageReceived += OnWebMessageReceived;
            _webView.CoreWebView2.NavigationStarting += (_, args) => _address.Text = FriendlyAddress(args.Uri);
            _webView.CoreWebView2.NavigationCompleted += (_, _) => UpdateNavigationState();
            Navigate(_options.InitialTarget);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            ShowRuntimeError();
        }
        catch (Exception exception)
        {
            MessageBox.Show(this, exception.Message, "Lab Robot could not start", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private void Navigate(string target)
    {
        if (_webView.CoreWebView2 is null) return;
        _activeTarget = _options.Targets.ContainsKey(target) ? target : "home";
        UpdateActiveNavigation();
        if (_activeTarget == "home")
        {
            _address.Text = "App home";
            _webView.NavigateToString(BuildHomeHtml());
            return;
        }

        var destination = _options.Targets[_activeTarget];
        if (_activeTarget == "lab" && !string.IsNullOrWhiteSpace(_options.PortalToken))
        {
            destination = destination.TrimEnd('/') + "/#authToken=" + Uri.EscapeDataString(_options.PortalToken);
        }
        _webView.CoreWebView2.Navigate(destination);
    }

    private void OnWebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        try
        {
            using var message = JsonDocument.Parse(args.WebMessageAsJson);
            if (message.RootElement.TryGetProperty("target", out var target))
            {
                Navigate(target.GetString() ?? "home");
            }
        }
        catch (JsonException)
        {
            // Ignore messages not emitted by the trusted local home document.
        }
    }

    private void UpdateNavigationState()
    {
        _backButton.Enabled = _webView.CanGoBack;
        _forwardButton.Enabled = _webView.CanGoForward;
    }

    private void UpdateActiveNavigation()
    {
        foreach (var (key, button) in _navigationButtons)
        {
            button.BackColor = key.Equals(_activeTarget, StringComparison.OrdinalIgnoreCase)
                ? Color.FromArgb(38, 38, 38)
                : Rail;
            button.ForeColor = key.Equals(_activeTarget, StringComparison.OrdinalIgnoreCase)
                ? Color.FromArgb(72, 171, 255)
                : Color.FromArgb(210, 210, 210);
        }
    }

    private void OpenExternally()
    {
        var source = _webView.Source?.ToString();
        if (string.IsNullOrWhiteSpace(source) || source == "about:blank") return;
        Process.Start(new ProcessStartInfo(source) { UseShellExecute = true });
    }

    private static string FriendlyAddress(string address)
    {
        if (address == "about:blank") return "App home";
        return Uri.TryCreate(address, UriKind.Absolute, out var uri)
            ? $"{uri.Host}{uri.AbsolutePath}"
            : address;
    }

    private void ShowRuntimeError()
    {
        _webView.Visible = false;
        var message = new Label
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(48),
            BackColor = Shell,
            ForeColor = Color.White,
            Font = new Font("Segoe UI", 13F),
            Text = "Microsoft Edge WebView2 Runtime is required.\n\nInstall the Evergreen Runtime, then reopen Lab Robot Windows App.\n\nhttps://developer.microsoft.com/microsoft-edge/webview2/",
        };
        _webView.Parent?.Controls.Add(message);
        message.BringToFront();
    }

    private string BuildHomeHtml()
    {
        var cards = new[]
        {
            new { Key = "claude", Mark = "AI", Name = "Claude Studio", Vendor = "Anthropic", Color = "#b54f35", Wide = false },
            new { Key = "veo", Mark = "VEO", Name = "Google Veo", Vendor = "Google Flow", Color = "#2d62b8", Wide = false },
            new { Key = "copilot", Mark = "{ }", Name = "Microsoft Copilot", Vendor = "Microsoft", Color = "#603bb3", Wide = true },
            new { Key = "lab", Mark = "▱", Name = "Lab Robot Control", Vendor = "Strat-Aqorynth", Color = "#0878bd", Wide = true },
        };
        var cardHtml = string.Join("", cards.Select(card => $"""
            <button class="card" onclick="openApp('{card.Key}')" aria-label="Open {card.Name}">
              <span class="art" style="background:{card.Color}"><b class="{(card.Wide ? "symbol" : "")}">{card.Mark}</b></span>
              <span class="meta"><strong>{card.Name}</strong><em>{card.Vendor}</em><small>Launch in workspace&nbsp; ↗</small></span>
            </button>
            """));
        return $$$"""
            <!doctype html>
            <html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
            <style>
              *{box-sizing:border-box} body{margin:0;background:#181818;color:#f5f5f5;font-family:Segoe UI,Arial,sans-serif}
              main{padding:42px 42px 70px;min-height:100vh} .eyebrow{margin:0 0 10px;color:#4e9fe6;font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase}
              h1{font-size:30px;margin:0 0 8px;line-height:1.15} .intro{margin:0;color:#a5a5a5;font-size:15px}
              .grid{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:54px;max-width:1030px;margin:38px auto 0}
              .card{padding:0;border:0;border-radius:11px;overflow:hidden;background:#292929;color:#fff;text-align:left;cursor:pointer;box-shadow:0 4px 16px #0003;transition:transform .16s,box-shadow .16s}
              .card:hover,.card:focus-visible{transform:translateY(-4px);box-shadow:0 12px 30px #0008;outline:2px solid #4e9fe6;outline-offset:3px}
              .art{height:138px;display:grid;place-items:center}.art b{font-size:38px}.art .symbol{font-family:Consolas,monospace;font-weight:500}
              .meta{display:flex;min-height:91px;padding:13px 15px 12px;flex-direction:column}.meta strong{font-size:17px;margin-bottom:4px}.meta em{font-size:11px;color:#57aaf3;font-style:normal}.meta small{margin-top:auto;color:#bdbdbd;font-size:11px}
              @media(max-width:1050px){.grid{grid-template-columns:repeat(2,minmax(220px,1fr));gap:28px}} @media(max-width:650px){main{padding:28px 22px}.grid{grid-template-columns:1fr}}
            </style></head><body><main>
              <p class="eyebrow">Strat-Aqorynth AI Workspaces</p><h1>Apps</h1>
              <p class="intro">Open AI studios and Lab Robot operations in one native Windows workspace.</p>
              <section class="grid">{{{cardHtml}}}</section>
            </main><script>function openApp(target){window.chrome.webview.postMessage({type:'navigate',target});}</script></body></html>
            """;
    }
}

internal sealed class RoundLabel : Label
{
    public Color BorderColor { get; init; } = Color.Gray;

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using var background = new SolidBrush(BackColor);
        using var border = new Pen(BorderColor, 1.5F);
        var bounds = new RectangleF(1, 1, Width - 3, Height - 3);
        e.Graphics.FillEllipse(background, bounds);
        e.Graphics.DrawEllipse(border, bounds);
        TextRenderer.DrawText(e.Graphics, Text, Font, ClientRectangle, ForeColor,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
    }
}
