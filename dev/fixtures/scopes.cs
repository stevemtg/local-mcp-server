using System;
using System.Collections.Generic;

namespace Sample
{
    public sealed class Renderer
    {
        public Options Settings { get; set; } = new Options
        {
            Count = 2
        };

        private void Prepare()
        {
            var settings = new Options
            {
                Count = 3,
                Values = new List<int> { 1, 2 }
            };
            Render(true);
        }

        private void Render(bool enabled)
        {
            if (enabled)
            {
                for (int i = 0; i < 2; i++)
                {
                    Console.WriteLine("{ ignored }"); // }
                    Render(false);
                }
            }
        }

        private void Refresh()
        {
            Render(false);
        }
    }
}
