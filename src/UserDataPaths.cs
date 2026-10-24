using System.IO;

namespace AgentProxy
{
    internal static class UserDataPaths
    {
        public static readonly string Directory = AppIdentity.DataDirectory;

        public static string File(string name)
        {
            return Path.Combine(Directory, name);
        }

        public static void Ensure()
        {
            System.IO.Directory.CreateDirectory(Directory);
        }
    }
}
