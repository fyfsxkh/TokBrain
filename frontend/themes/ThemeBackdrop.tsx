"use client";

import { useEffect, useRef, useState } from "react";

import { useTheme } from "./ThemeProvider";

function observeMedia(query: string, onChange: () => void) {
  const media = window.matchMedia(query);
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

export function ThemeBackdrop() {
  const { wallpaper, motionEnabled } = useTheme();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [motionAllowed, setMotionAllowed] = useState(false);
  const [failedWallpaperKey, setFailedWallpaperKey] = useState<string | null>(null);
  const [readyWallpaperKey, setReadyWallpaperKey] = useState<string | null>(null);
  const wallpaperKey = !wallpaper
    ? "none"
    : wallpaper.kind === "video"
      ? `video:${wallpaper.poster}:${wallpaper.sources.map((source) => source.src).join("|")}`
      : `${wallpaper.kind}:${wallpaper.src}`;
  const mediaFailed = failedWallpaperKey === wallpaperKey;
  const mediaReady = readyWallpaperKey === wallpaperKey;

  useEffect(() => {
    const update = () => {
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const narrow = window.matchMedia("(max-width: 760px)").matches;
      const forced = window.matchMedia("(forced-colors: active)").matches;
      setMotionAllowed(motionEnabled && !reduced && !narrow && !forced);
    };
    update();
    const cleanups = [
      observeMedia("(prefers-reduced-motion: reduce)", update),
      observeMedia("(max-width: 760px)", update),
      observeMedia("(forced-colors: active)", update),
    ];
    return () => {
      for (const cleanup of cleanups) cleanup();
    };
  }, [motionEnabled]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const syncPlayback = () => {
      if (!motionAllowed || mediaFailed || document.hidden) {
        video.pause();
        return;
      }
      // Autoplay policy rejection is temporary: keep the decoded first frame
      // and retry when the page becomes visible instead of marking the asset bad.
      void video.play().catch(() => undefined);
    };
    syncPlayback();
    document.addEventListener("visibilitychange", syncPlayback);
    return () => document.removeEventListener("visibilitychange", syncPlayback);
  }, [mediaFailed, motionAllowed, wallpaperKey]);

  if (!wallpaper) return null;

  const poster = (
    <img
      className="theme-wallpaper-media theme-wallpaper-poster"
      src={wallpaper.poster}
      alt=""
      aria-hidden="true"
    />
  );
  const builtinVideo = wallpaper.kind === "video"
    && wallpaper.sources.every((source) => source.src.startsWith("/themes/dynamic/"));

  return (
    <div className="theme-wallpaper-layer" aria-hidden="true">
      {wallpaper.kind === "image" ? (
        <img className="theme-wallpaper-media" src={wallpaper.src} alt="" onError={() => setFailedWallpaperKey(wallpaperKey)} />
      ) : (
        <>
          {poster}
          {wallpaper.kind === "animated-image" && motionAllowed && !mediaFailed && (
            <img
              className={`theme-wallpaper-media theme-wallpaper-motion ${mediaReady ? "ready" : ""}`}
              src={wallpaper.src}
              alt=""
              onLoad={() => setReadyWallpaperKey(wallpaperKey)}
              onError={() => setFailedWallpaperKey(wallpaperKey)}
            />
          )}
          {wallpaper.kind === "video" && motionAllowed && !mediaFailed && (
            <video
              key={wallpaperKey}
              ref={videoRef}
              className={`theme-wallpaper-media theme-wallpaper-motion ${mediaReady ? "ready" : ""}`}
              poster={wallpaper.poster}
              muted
              loop
              playsInline
              autoPlay
              preload={builtinVideo ? "auto" : "metadata"}
              tabIndex={-1}
              onLoadedData={() => setReadyWallpaperKey(wallpaperKey)}
              onError={() => setFailedWallpaperKey(wallpaperKey)}
            >
              {wallpaper.sources.map((source) => (
                <source key={`${source.type}:${source.src}`} src={source.src} type={source.type} />
              ))}
            </video>
          )}
        </>
      )}
      <span className="theme-wallpaper-overlay" />
    </div>
  );
}
