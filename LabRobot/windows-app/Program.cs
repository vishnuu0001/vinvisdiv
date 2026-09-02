using System.Web;

namespace LabRobot.WindowsApp;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new WorkspaceForm(WorkspaceOptions.FromArguments(args)));
    }
}

internal sealed record WorkspaceOptions(
    string InitialTarget,
    string? PortalToken,
    IReadOnlyDictionary<string, string> Targets)
{
    public static WorkspaceOptions FromArguments(string[] args)
    {
        var targets = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["home"] = "app://home",
            ["lab"] = Environment.GetEnvironmentVariable("LAB_ROBOT_WEB_URL") ?? "http://localhost:7000/",
            ["claude"] = Environment.GetEnvironmentVariable("LAB_ROBOT_CLAUDE_URL") ?? "https://claude.ai/",
            ["veo"] = Environment.GetEnvironmentVariable("LAB_ROBOT_VEO_URL") ?? "https://labs.google/fx/tools/flow",
            ["copilot"] = Environment.GetEnvironmentVariable("LAB_ROBOT_COPILOT_URL") ?? "https://copilot.microsoft.com/",
        };

        var initialTarget = "home";
        string? token = null;
        var launchArgument = args.FirstOrDefault(value =>
            Uri.TryCreate(value, UriKind.Absolute, out var candidate)
            && candidate.Scheme.Equals("stratiq-labrobot", StringComparison.OrdinalIgnoreCase));

        if (launchArgument is not null && Uri.TryCreate(launchArgument, UriKind.Absolute, out var launchUri))
        {
            var requested = launchUri.AbsolutePath.Trim('/');
            if (string.IsNullOrWhiteSpace(requested) && !launchUri.Host.Equals("open", StringComparison.OrdinalIgnoreCase))
            {
                requested = launchUri.Host;
            }

            initialTarget = requested.Equals("lab-robot", StringComparison.OrdinalIgnoreCase)
                ? "lab"
                : targets.ContainsKey(requested) ? requested : "home";
            token = HttpUtility.ParseQueryString(launchUri.Query).Get("token");
        }

        return new WorkspaceOptions(initialTarget, token, targets);
    }
}
