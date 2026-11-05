export function uniquePortAllocator(allocate) {
  const allocated = new Set();
  return async () => {
    for (let attempt = 0; attempt < 32; attempt++) {
      const port = await allocate();
      if (allocated.has(port)) continue;
      allocated.add(port);
      return port;
    }
    throw new Error('Could not allocate a distinct fixture port after 32 attempts');
  };
}
