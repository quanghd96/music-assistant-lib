"""Spotify musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import time
from json.decoder import JSONDecodeError
from tempfile import gettempdir
from typing import AsyncGenerator, List, Optional, Tuple

import aiohttp
from asyncio_throttle import Throttler

from music_assistant.helpers.app_vars import (  # noqa # pylint: disable=no-name-in-module
    app_var,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.enums import MusicProviderFeature, ProviderType
from music_assistant.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    MediaQuality,
    MediaType,
    Playlist,
    StreamDetails,
    Track,
)
from music_assistant.models.music_provider import MusicProvider

CACHE_DIR = gettempdir()


class SpotifyProvider(MusicProvider):
    """Implementation of a Spotify MusicProvider."""

    _attr_type = ProviderType.SPOTIFY
    _attr_name = "Spotify"
    _auth_token = None
    _sp_user = None
    _librespot_bin = None
    _throttler = Throttler(rate_limit=4, period=1)
    _cache_dir = CACHE_DIR
    _ap_workaround = False

    @property
    def supported_features(self) -> Tuple[MusicProviderFeature]:
        """Return the features supported by this MusicProvider."""
        return (
            MusicProviderFeature.LIBRARY_ARTISTS,
            MusicProviderFeature.LIBRARY_ALBUMS,
            MusicProviderFeature.LIBRARY_TRACKS,
            MusicProviderFeature.LIBRARY_PLAYLISTS,
            MusicProviderFeature.LIBRARY_ARTISTS_EDIT,
            MusicProviderFeature.LIBRARY_ALBUMS_EDIT,
            MusicProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            MusicProviderFeature.LIBRARY_TRACKS_EDIT,
            MusicProviderFeature.PLAYLIST_TRACKS_EDIT,
            MusicProviderFeature.BROWSE,
            MusicProviderFeature.SEARCH,
            MusicProviderFeature.ARTIST_ALBUMS,
            MusicProviderFeature.ARTIST_TOPTRACKS,
            MusicProviderFeature.SIMILAR_TRACKS,
        )

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""
        if not self.config.enabled:
            return False
        # try to get a token, raise if that fails
        self._cache_dir = os.path.join(CACHE_DIR, self.id)
        # try login which will raise if it fails
        await self.login()
        return True

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        result = []
        searchtypes = []
        if MediaType.ARTIST in media_types:
            searchtypes.append("artist")
        if MediaType.ALBUM in media_types:
            searchtypes.append("album")
        if MediaType.TRACK in media_types:
            searchtypes.append("track")
        if MediaType.PLAYLIST in media_types:
            searchtypes.append("playlist")
        searchtype = ",".join(searchtypes)
        search_query = search_query.replace("'", "")
        if searchresult := await self._get_data(
            "search", q=search_query, type=searchtype, limit=limit
        ):
            if "artists" in searchresult:
                result += [
                    await self._parse_artist(item)
                    for item in searchresult["artists"]["items"]
                    if (item and item["id"])
                ]
            if "albums" in searchresult:
                result += [
                    await self._parse_album(item)
                    for item in searchresult["albums"]["items"]
                    if (item and item["id"])
                ]
            if "tracks" in searchresult:
                result += [
                    await self._parse_track(item)
                    for item in searchresult["tracks"]["items"]
                    if (item and item["id"])
                ]
            if "playlists" in searchresult:
                result += [
                    await self._parse_playlist(item)
                    for item in searchresult["playlists"]["items"]
                    if (item and item["id"])
                ]
        return result

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from spotify."""
        endpoint = "me/following"
        while True:
            spotify_artists = await self._get_data(
                endpoint,
                type="artist",
                limit=50,
            )
            for item in spotify_artists["artists"]["items"]:
                if item and item["id"]:
                    yield await self._parse_artist(item)
            if spotify_artists["artists"]["next"]:
                endpoint = spotify_artists["artists"]["next"]
                endpoint = endpoint.replace("https://api.spotify.com/v1/", "")
            else:
                break

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        for item in await self._get_all_items("me/albums"):
            if item["album"] and item["album"]["id"]:
                yield await self._parse_album(item["album"])

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        for item in await self._get_all_items("me/tracks"):
            if item and item["track"]["id"]:
                yield await self._parse_track(item["track"])

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        for item in await self._get_all_items("me/playlists"):
            if item and item["id"]:
                yield await self._parse_playlist(item)

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data(f"artists/{prov_artist_id}")
        return await self._parse_artist(artist_obj) if artist_obj else None

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data(f"albums/{prov_album_id}")
        return await self._parse_album(album_obj) if album_obj else None

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data(f"tracks/{prov_track_id}")
        return await self._parse_track(track_obj) if track_obj else None

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = await self._get_data(f"playlists/{prov_playlist_id}")
        return await self._parse_playlist(playlist_obj) if playlist_obj else None

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        """Get all album tracks for given album id."""
        return [
            await self._parse_track(item)
            for item in await self._get_all_items(f"albums/{prov_album_id}/tracks")
            if (item and item["id"])
        ]

    async def get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        count = 0
        result = []
        for item in await self._get_all_items(
            f"playlists/{prov_playlist_id}/tracks",
        ):
            if not (item and item["track"] and item["track"]["id"]):
                continue
            track = await self._parse_track(item["track"])
            # use count as position
            track.position = count
            result.append(track)
            count += 1
        return result

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        """Get a list of all albums for the given artist."""
        return [
            await self._parse_album(item)
            for item in await self._get_all_items(
                f"artists/{prov_artist_id}/albums?include_groups=album,single,compilation"
            )
            if (item and item["id"])
        ]

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        artist = await self.get_artist(prov_artist_id)
        endpoint = f"artists/{prov_artist_id}/top-tracks"
        items = await self._get_data(endpoint)
        return [
            await self._parse_track(item, artist=artist)
            for item in items["tracks"]
            if (item and item["id"])
        ]

    async def library_add(self, prov_item_id, media_type: MediaType):
        """Add item to library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await self._put_data(
                "me/following", {"ids": prov_item_id, "type": "artist"}
            )
        elif media_type == MediaType.ALBUM:
            result = await self._put_data("me/albums", {"ids": prov_item_id})
        elif media_type == MediaType.TRACK:
            result = await self._put_data("me/tracks", {"ids": prov_item_id})
        elif media_type == MediaType.PLAYLIST:
            result = await self._put_data(
                f"playlists/{prov_item_id}/followers", data={"public": False}
            )
        return result

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await self._delete_data(
                "me/following", {"ids": prov_item_id, "type": "artist"}
            )
        elif media_type == MediaType.ALBUM:
            result = await self._delete_data("me/albums", {"ids": prov_item_id})
        elif media_type == MediaType.TRACK:
            result = await self._delete_data("me/tracks", {"ids": prov_item_id})
        elif media_type == MediaType.PLAYLIST:
            result = await self._delete_data(f"playlists/{prov_item_id}/followers")
        return result

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ):
        """Add track(s) to playlist."""
        track_uris = []
        for track_id in prov_track_ids:
            track_uris.append(f"spotify:track:{track_id}")
        data = {"uris": track_uris}
        return await self._post_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: Tuple[int]
    ) -> None:
        """Remove track(s) from playlist."""
        track_uris = []
        for track in await self.get_playlist_tracks(prov_playlist_id):
            if track.position in positions_to_remove:
                track_uris.append({"uri": f"spotify:track:{track.item_id}"})
            if len(track_uris) == positions_to_remove:
                break
        data = {"tracks": track_uris}
        return await self._delete_data(
            f"playlists/{prov_playlist_id}/tracks", data=data
        )

    async def get_similar_tracks(self, prov_track_id, limit=25) -> List[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        endpoint = "recommendations"
        items = await self._get_data(endpoint, seed_tracks=prov_track_id, limit=limit)
        return [
            await self._parse_track(item)
            for item in items["tracks"]
            if (item and item["id"])
        ]

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # make sure a valid track is requested.
        track = await self.get_track(item_id)
        if not track:
            raise MediaNotFoundError(f"track {item_id} not found")
        # make sure that the token is still valid by just requesting it
        await self.login()
        return StreamDetails(
            item_id=track.item_id,
            provider=self.type,
            content_type=ContentType.OGG,
            duration=track.duration,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        # make sure that the token is still valid by just requesting it
        await self.login()
        librespot = await self.get_librespot_binary()
        args = [
            librespot,
            "-c",
            self._cache_dir,
            "--pass-through",
            "-b",
            "320",
            "--single-track",
            f"spotify://track:{streamdetails.item_id}",
        ]
        if seek_position:
            args += ["--start-position", str(int(seek_position))]
        if self._ap_workaround:
            args += ["--ap-port", "12345"]
        bytes_sent = 0
        async with AsyncProcess(args) as librespot_proc:
            async for chunk in librespot_proc.iter_any():
                yield chunk
                bytes_sent += len(chunk)

        if bytes_sent == 0 and not self._ap_workaround:
            # AP resolve failure
            # https://github.com/librespot-org/librespot/issues/972
            # retry with ap-port set to invalid value, which will force fallback
            args += ["--ap-port", "12345"]
            async with AsyncProcess(args) as librespot_proc:
                async for chunk in librespot_proc.iter_any(64000):
                    yield chunk
            self._ap_workaround = True

    async def _parse_artist(self, artist_obj):
        """Parse spotify artist object to generic layout."""
        artist = Artist(
            item_id=artist_obj["id"], provider=self.type, name=artist_obj["name"]
        )
        artist.add_provider_id(
            MediaItemProviderId(
                item_id=artist_obj["id"],
                prov_type=self.type,
                prov_id=self.id,
                url=artist_obj["external_urls"]["spotify"],
            )
        )
        if "genres" in artist_obj:
            artist.metadata.genres = set(artist_obj["genres"])
        if artist_obj.get("images"):
            for img in artist_obj["images"]:
                img_url = img["url"]
                if "2a96cbd8b46e442fc41c2b86b821562f" not in img_url:
                    artist.metadata.images = [MediaItemImage(ImageType.THUMB, img_url)]
                    break
        return artist

    async def _parse_album(self, album_obj: dict):
        """Parse spotify album object to generic layout."""
        name, version = parse_title_and_version(album_obj["name"])
        album = Album(
            item_id=album_obj["id"], provider=self.type, name=name, version=version
        )
        for artist_obj in album_obj["artists"]:
            album.artists.append(await self._parse_artist(artist_obj))
        if album_obj["album_type"] == "single":
            album.album_type = AlbumType.SINGLE
        elif album_obj["album_type"] == "compilation":
            album.album_type = AlbumType.COMPILATION
        elif album_obj["album_type"] == "album":
            album.album_type = AlbumType.ALBUM
        if "genres" in album_obj:
            album.metadata.genre = set(album_obj["genres"])
        if album_obj.get("images"):
            album.metadata.images = [
                MediaItemImage(ImageType.THUMB, album_obj["images"][0]["url"])
            ]
        if "external_ids" in album_obj and album_obj["external_ids"].get("upc"):
            album.upc = album_obj["external_ids"]["upc"]
        if "label" in album_obj:
            album.metadata.label = album_obj["label"]
        if album_obj.get("release_date"):
            album.year = int(album_obj["release_date"].split("-")[0])
        if album_obj.get("copyrights"):
            album.metadata.copyright = album_obj["copyrights"][0]["text"]
        if album_obj.get("explicit"):
            album.metadata.explicit = album_obj["explicit"]
        album.add_provider_id(
            MediaItemProviderId(
                item_id=album_obj["id"],
                prov_type=self.type,
                prov_id=self.id,
                quality=MediaQuality.LOSSY_OGG,
                url=album_obj["external_urls"]["spotify"],
            )
        )
        return album

    async def _parse_track(self, track_obj, artist=None):
        """Parse spotify track object to generic layout."""
        name, version = parse_title_and_version(track_obj["name"])
        track = Track(
            item_id=track_obj["id"],
            provider=self.type,
            name=name,
            version=version,
            duration=track_obj["duration_ms"] / 1000,
            disc_number=track_obj["disc_number"],
            track_number=track_obj["track_number"],
            position=track_obj.get("position"),
        )
        if artist:
            track.artists.append(artist)
        for track_artist in track_obj.get("artists", []):
            artist = await self._parse_artist(track_artist)
            if artist and artist.item_id not in {x.item_id for x in track.artists}:
                track.artists.append(artist)

        track.metadata.explicit = track_obj["explicit"]
        if "preview_url" in track_obj:
            track.metadata.preview = track_obj["preview_url"]
        if "external_ids" in track_obj and "isrc" in track_obj["external_ids"]:
            track.isrc = track_obj["external_ids"]["isrc"]
        if "album" in track_obj:
            track.album = await self._parse_album(track_obj["album"])
            if track_obj["album"].get("images"):
                track.metadata.images = [
                    MediaItemImage(
                        ImageType.THUMB, track_obj["album"]["images"][0]["url"]
                    )
                ]
        if track_obj.get("copyright"):
            track.metadata.copyright = track_obj["copyright"]
        if track_obj.get("explicit"):
            track.metadata.explicit = True
        if track_obj.get("popularity"):
            track.metadata.popularity = track_obj["popularity"]
        track.add_provider_id(
            MediaItemProviderId(
                item_id=track_obj["id"],
                prov_type=self.type,
                prov_id=self.id,
                quality=MediaQuality.LOSSY_OGG,
                url=track_obj["external_urls"]["spotify"],
                available=not track_obj["is_local"] and track_obj["is_playable"],
            )
        )
        return track

    async def _parse_playlist(self, playlist_obj):
        """Parse spotify playlist object to generic layout."""
        playlist = Playlist(
            item_id=playlist_obj["id"],
            provider=self.type,
            name=playlist_obj["name"],
            owner=playlist_obj["owner"]["display_name"],
        )
        playlist.add_provider_id(
            MediaItemProviderId(
                item_id=playlist_obj["id"],
                prov_type=self.type,
                prov_id=self.id,
                url=playlist_obj["external_urls"]["spotify"],
            )
        )
        playlist.is_editable = (
            playlist_obj["owner"]["id"] == self._sp_user["id"]
            or playlist_obj["collaborative"]
        )
        if playlist_obj.get("images"):
            playlist.metadata.images = [
                MediaItemImage(ImageType.THUMB, playlist_obj["images"][0]["url"])
            ]
        playlist.metadata.checksum = str(playlist_obj["snapshot_id"])
        return playlist

    async def login(self) -> dict:
        """Log-in Spotify and return tokeninfo."""
        # return existing token if we have one in memory
        if (
            self._auth_token
            and os.path.isdir(self._cache_dir)
            and (self._auth_token["expiresAt"] > int(time.time()) + 20)
        ):
            return self._auth_token
        tokeninfo, userinfo = None, self._sp_user
        if not self.config.username or not self.config.password:
            raise LoginFailed("Invalid login credentials")
        # retrieve token with librespot
        retries = 0
        while retries < 20:
            try:
                retries += 1
                if not tokeninfo:
                    tokeninfo = await asyncio.wait_for(self._get_token(), 5)
                if tokeninfo and not userinfo:
                    userinfo = await asyncio.wait_for(
                        self._get_data("me", tokeninfo=tokeninfo), 5
                    )
                if tokeninfo and userinfo:
                    # we have all info we need!
                    break
                if retries > 2:
                    # switch to ap workaround after 2 retries
                    self._ap_workaround = True
            except asyncio.exceptions.TimeoutError:
                await asyncio.sleep(2)
        if tokeninfo and userinfo:
            self._auth_token = tokeninfo
            self._sp_user = userinfo
            self.mass.metadata.preferred_language = userinfo["country"]
            self.logger.info("Succesfully logged in to Spotify as %s", userinfo["id"])
            self._auth_token = tokeninfo
            return tokeninfo
        if tokeninfo and not userinfo:
            raise LoginFailed(
                "Unable to retrieve userdetails from Spotify API - probably just a temporary error"
            )
        if self.config.username.isnumeric():
            # a spotify free/basic account can be recognized when
            # the username consists of numbers only - check that here
            # an integer can be parsed of the username, this is a free account
            raise LoginFailed("Only Spotify Premium accounts are supported")
        raise LoginFailed(f"Login failed for user {self.config.username}")

    async def _get_token(self):
        """Get spotify auth token with librespot bin."""
        time_start = time.time()
        # authorize with username and password (NOTE: this can also be Spotify Connect)
        args = [
            await self.get_librespot_binary(),
            "-O",
            "-c",
            self._cache_dir,
            "-a",
            "-u",
            self.config.username,
            "-p",
            self.config.password,
        ]
        librespot = await asyncio.create_subprocess_exec(*args)
        await librespot.wait()
        # get token with (authorized) librespot
        scopes = [
            "user-read-playback-state",
            "user-read-currently-playing",
            "user-modify-playback-state",
            "playlist-read-private",
            "playlist-read-collaborative",
            "playlist-modify-public",
            "playlist-modify-private",
            "user-follow-modify",
            "user-follow-read",
            "user-library-read",
            "user-library-modify",
            "user-read-private",
            "user-read-email",
            "user-read-birthdate",
            "user-top-read",
        ]
        scope = ",".join(scopes)
        args = [
            await self.get_librespot_binary(),
            "-O",
            "-t",
            "--client-id",
            app_var(2),
            "--scope",
            scope,
            "-c",
            self._cache_dir,
        ]
        if self._ap_workaround:
            args += ["--ap-port", "12345"]
        librespot = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await librespot.communicate()
        duration = round(time.time() - time_start, 2)
        try:
            result = json.loads(stdout)
        except JSONDecodeError:
            self.logger.warning(
                "Error while retrieving Spotify token after %s seconds, details: %s",
                duration,
                stdout.decode(),
            )
            return None
        self.logger.debug(
            "Retrieved Spotify token using librespot in %s seconds",
            duration,
        )
        # transform token info to spotipy compatible format
        if result and "accessToken" in result:
            tokeninfo = result
            tokeninfo["expiresAt"] = tokeninfo["expiresIn"] + int(time.time())
            return tokeninfo
        return None

    async def _get_all_items(self, endpoint, key="items", **kwargs) -> List[dict]:
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        all_items = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            offset += limit
            if not result or key not in result or not result[key]:
                break
            for item in result[key]:
                item["position"] = len(all_items) + 1
                all_items.append(item)
            if len(result[key]) < limit:
                break
        return all_items

    async def _get_data(self, endpoint, tokeninfo: Optional[dict] = None, **kwargs):
        """Get data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        kwargs["market"] = "from_token"
        kwargs["country"] = "from_token"
        if tokeninfo is None:
            tokeninfo = await self.login()
        headers = {"Authorization": f'Bearer {tokeninfo["accessToken"]}'}
        async with self._throttler:
            time_start = time.time()
            try:
                async with self.mass.http_session.get(
                    url, headers=headers, params=kwargs, verify_ssl=False, timeout=120
                ) as response:
                    result = await response.json()
                    if "error" in result or (
                        "status" in result and "error" in result["status"]
                    ):
                        self.logger.error("%s - %s", endpoint, result)
                        return None
            except (
                aiohttp.ContentTypeError,
                JSONDecodeError,
            ) as err:
                self.logger.error("%s - %s", endpoint, str(err))
                return None
            finally:
                self.logger.debug(
                    "Processing GET/%s took %s seconds",
                    endpoint,
                    round(time.time() - time_start, 2),
                )
            return result

    async def _delete_data(self, endpoint, data=None, **kwargs):
        """Delete data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.login()
        if not token:
            return None
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self.mass.http_session.delete(
            url, headers=headers, params=kwargs, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def _put_data(self, endpoint, data=None, **kwargs):
        """Put data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.login()
        if not token:
            return None
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self.mass.http_session.put(
            url, headers=headers, params=kwargs, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def _post_data(self, endpoint, data=None, **kwargs):
        """Post data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.login()
        if not token:
            return None
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self.mass.http_session.post(
            url, headers=headers, params=kwargs, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def get_librespot_binary(self):
        """Find the correct librespot binary belonging to the platform."""
        if self._librespot_bin is not None:
            return self._librespot_bin

        async def check_librespot(librespot_path: str) -> str | None:
            try:
                librespot = await asyncio.create_subprocess_exec(
                    *[librespot_path, "--check"], stdout=asyncio.subprocess.PIPE
                )
                stdout, _ = await librespot.communicate()
                if (
                    librespot.returncode == 0
                    and b"ok spotty" in stdout
                    and b"using librespot" in stdout
                ):
                    self._librespot_bin = librespot_path
                    return librespot_path
            except OSError:
                return None

        base_path = os.path.join(os.path.dirname(__file__), "librespot")
        if platform.system() == "Windows":
            if librespot := await check_librespot(
                os.path.join(base_path, "windows", "librespot.exe")
            ):
                return librespot
        if platform.system() == "Darwin":
            # macos binary is x86_64 intel
            if librespot := await check_librespot(
                os.path.join(base_path, "osx", "librespot")
            ):
                return librespot

        if platform.system() == "FreeBSD":
            # FreeBSD binary is x86_64 intel
            if librespot := await check_librespot(
                os.path.join(base_path, "freebsd", "librespot")
            ):
                return librespot

        if platform.system() == "Linux":
            architecture = platform.machine()
            if architecture in ["AMD64", "x86_64"]:
                # generic linux x86_64 binary
                if librespot := await check_librespot(
                    os.path.join(
                        base_path,
                        "linux",
                        "librespot-x86_64",
                    )
                ):
                    return librespot

            # arm architecture... try all options one by one...
            for arch in ["aarch64", "armv7", "armhf", "arm"]:
                if librespot := await check_librespot(
                    os.path.join(
                        base_path,
                        "linux",
                        f"librespot-{arch}",
                    )
                ):
                    return librespot

        raise RuntimeError(
            f"Unable to locate Libespot for {platform.system()} ({platform.machine()})"
        )
