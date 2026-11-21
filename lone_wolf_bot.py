"""
Lone Wolf Labs bot v8 - SURVIVOR VULTURE
for Builders - Mech Royale (mechfight/1).

Core doctrine:
1. Stay alive first.
2. Avoid early 1v1s.
3. Third-party weakened, distracted, or zone-trapped enemies.
4. Hug the safe edge of the shrinking zone.
5. Break line of sight and regenerate after taking damage.
6. Farm weapons and cubes only when safe.
7. Avoid noisy multi-bot fights when weak or under-equipped.
8. Never crash, never stall, and always return an action.
"""

from __future__ import annotations

import array
import json
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf


Position = Tuple[float, float]
Vector = Tuple[float, float]


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _dist2d(a: List[float], b: List[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _normalize(vector: Vector) -> Vector:
    length = math.hypot(vector[0], vector[1])

    if length < 1e-9:
        return (0.0, 0.0)

    return (vector[0] / length, vector[1] / length)


class Terrain:
    """Loads terrain data for basic ground-height and reachability checks."""

    def __init__(self, map_dir: str) -> None:
        self.ok = False

        try:
            base = os.path.join(map_dir, "v2")

            with open(os.path.join(base, "world.json"), "r", encoding="utf-8") as file:
                meta = json.load(file)["terrain"]

            self.min_x = float(meta["minX"])
            self.min_y = float(meta["minY"])
            self.step = float(meta["step"])
            self.nx = int(meta["nx"])
            self.ny = int(meta["ny"])

            data = array.array("h")

            with open(
                os.path.join(base, str(meta.get("file", "terrain.i16"))),
                "rb",
            ) as file:
                data.fromfile(file, self.nx * self.ny)

            if sys.byteorder == "big":
                data.byteswap()

            self.data = data
            self.ok = True

        except Exception:
            self.ok = False

    def ground(self, x: float, y: float) -> Optional[float]:
        if not self.ok:
            return None

        index_x = max(
            0,
            min(self.nx - 1, int(round((x - self.min_x) / self.step))),
        )
        index_y = max(
            0,
            min(self.ny - 1, int(round((y - self.min_y) / self.step))),
        )

        return self.data[index_y * self.nx + index_x] / 100.0


class Bot:
    name = "lone-wolf-v8-survivor-vulture"

    def __init__(self, hello: mf.Hello) -> None:
        config = hello.get("config", {})
        loot_config = config.get("loot", {})

        self.pickup_radius = float(loot_config.get("pickup_radius", 4.0))
        self.upgrades = config.get("upgrades", [])
        self.upgrade_priority: Dict[str, int] = {}

        for upgrade in self.upgrades:
            upgrade_id = str(upgrade.get("id", ""))
            effects = upgrade.get("effects", [])
            score = 0

            for effect in effects:
                path = str(effect.get("path", ""))

                if "armor" in upgrade_id or "rear" in path or "max_hp" in path or "regen" in path:
                    score += 35
                if "sensor" in upgrade_id or "vision" in path or "sound" in path:
                    score += 25
                if "speed" in path or "boost" in path or "energy" in path:
                    score += 20
                if "damage" in path or "rate" in path or "cooldown" in path:
                    score += 15

            self.upgrade_priority[upgrade_id] = score

        self.rng = random.Random(int(hello.get("seed", 1234567)))
        self.terrain = Terrain(str(hello.get("map", {}).get("dir", "/arena")))

        self.last_damage_time = -999.0
        self.last_damage_bearing: Optional[float] = None
        self.laser_charging = [False, False]
        self.skipped_pickups: Dict[int, float] = {}
        self.loot_memory: Optional[Dict[str, Any]] = None
        self.last_position: Optional[Tuple[float, float]] = None
        self.stuck_ticks = 0
        self.strafe_direction = self.rng.choice([-1.0, 1.0])
        self.strafe_until = 0.0
        self.target_id: Optional[int] = None
        self.target_until = 0.0

    def _choose_upgrade(self, offer: Dict[str, Any]) -> int:
        choices = offer.get("choices", [])

        if not choices:
            return 0

        best_index = 0
        best_score = -10**9

        for index, choice in enumerate(choices):
            upgrade_id = str(choice.get("id", ""))
            description = str(choice.get("desc", "")).lower()
            score = self.upgrade_priority.get(upgrade_id, 0)

            if "armor" in description or "hp" in description or "regen" in description:
                score += 30
            if "vision" in description or "sensor" in description or "sound" in description:
                score += 20
            if "speed" in description or "energy" in description or "boost" in description:
                score += 15
            if "damage" in description or "rate" in description or "cooldown" in description:
                score += 10

            if score > best_score:
                best_score = score
                best_index = index

        return best_index

    def _zone_target(self, observation: mf.Observation, x: float, y: float) -> Tuple[float, float]:
        zone = observation["zone"]
        time = float(observation["t"])

        shrink_start = float(zone.get("shrink_start", time))
        shrink_end = float(zone.get("shrink_end", time + 1.0))

        current_center = zone.get("center", [0.0, 0.0])
        current_radius = float(zone.get("radius", 500.0))
        next_center = zone.get("next_center", current_center)
        next_radius = float(zone.get("next_radius", current_radius))

        if time >= shrink_start:
            if shrink_end > shrink_start:
                progress = max(
                    0.0,
                    min(1.0, (time - shrink_start) / (shrink_end - shrink_start)),
                )
            else:
                progress = 1.0

            center_x = current_center[0] + (next_center[0] - current_center[0]) * progress
            center_y = current_center[1] + (next_center[1] - current_center[1]) * progress
            radius = current_radius + (next_radius - current_radius) * progress
        else:
            center_x, center_y, radius = current_center[0], current_center[1], current_radius

        # Hug the safe edge rather than sitting in the exposed centre.
        edge_radius = max(5.0, radius * 0.80)

        delta_x = next_center[0] - center_x
        delta_y = next_center[1] - center_y
        delta_length = math.hypot(delta_x, delta_y)

        if delta_length > 1e-9:
            target_x = center_x + (delta_x / delta_length) * min(
                delta_length * 0.80,
                edge_radius * 0.60,
            )
            target_y = center_y + (delta_y / delta_length) * min(
                delta_length * 0.80,
                edge_radius * 0.60,
            )
        else:
            target_x, target_y = center_x, center_y

        current_distance = math.hypot(x - center_x, y - center_y)
        next_distance = math.hypot(x - next_center[0], y - next_center[1])

        if current_distance < edge_radius and next_distance > next_radius * 0.88:
            return float(next_center[0]), float(next_center[1])

        return float(target_x), float(target_y)

    def _has_line_of_sight(
        self,
        start: List[float],
        end: List[float],
        walls: List[Tuple[List[float], List[float]]],
    ) -> bool:
        """Conservative 2D line-of-sight test against available wall segments."""

        if not walls:
            return True

        start_x, start_y = start[0], start[1]
        end_x, end_y = end[0], end[1]

        for wall in walls:
            wall_start, wall_end = wall
            wall_x1, wall_y1 = wall_start[0], wall_start[1]
            wall_x2, wall_y2 = wall_end[0], wall_end[1]

            denominator = (
                (end_x - start_x) * (wall_y2 - wall_y1)
                - (end_y - start_y) * (wall_x2 - wall_x1)
            )

            if abs(denominator) < 1e-9:
                continue

            first = (
                (wall_x1 - start_x) * (wall_y2 - wall_y1)
                - (wall_y1 - start_y) * (wall_x2 - wall_x1)
            ) / denominator

            second = (
                (wall_x1 - start_x) * (end_y - start_y)
                - (wall_y1 - start_y) * (end_x - start_x)
            ) / denominator

            if 0.0 <= first <= 1.0 and 0.0 <= second <= 1.0:
                return False

        return True

    def _weapon_decision(
        self,
        slot: int,
        weapon: Optional[mf.WeaponObs],
        distance: float,
        has_target: bool,
        energy: float,
        target_has_mortar: bool,
    ) -> bool:
        if weapon is None or not has_target:
            self.laser_charging[slot] = False
            return False

        kind = weapon.get("kind")
        stats = weapon.get("stats", {})
        max_range = float(stats.get("range", 200.0))

        if kind == "gun":
            ammo = float(weapon.get("ammo", 0.0))
            reload_left = float(weapon.get("reload", 0.0))

            if reload_left > 0.0 or ammo <= 0.0:
                return False

            return distance <= max_range * 0.92

        if kind == "lightning":
            if energy < 12.0 or distance > min(42.0, max_range):
                return False

            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "mortar":
            if distance < 28.0 or distance > max_range:
                return False

            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "laser":
            charge_time = float(weapon.get("charge", 0.0))
            full_charge = float(stats.get("charge", 0.75))

            if distance > max_range:
                self.laser_charging[slot] = False
                return False

            release_charge = full_charge * (0.82 if target_has_mortar else 0.94)

            if not self.laser_charging[slot]:
                if float(weapon.get("cooldown", 0.0)) <= 0.05 and energy > 15.0:
                    self.laser_charging[slot] = True
                    return True

                return False

            if charge_time >= release_charge:
                self.laser_charging[slot] = False
                return False

            return True

        return False

    def act(self, observation: mf.Observation) -> mf.Action:
        try:
            return self._decide(observation)
        except Exception:
            # Never crash or freeze. Return a minimal valid action.
            try:
                return {"tick": observation["tick"]}
            except Exception:
                return {"tick": 0}

    def _decide(self, observation: mf.Observation) -> mf.Action:
        me = observation["self"]

        x, y, z = float(me["pos"][0]), float(me["pos"][1]), float(me["pos"][2])
        hp = float(me.get("hp", 1400.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))
        time = float(observation["t"])
        stats = me.get("stats", {})
        muzzle_z = z + float(stats.get("muzzle_height", 7.0))

        for event in observation.get("events", []):
            if event.get("type") == "damaged":
                self.last_damage_time = time

                if event.get("bearing") is not None:
                    self.last_damage_bearing = float(event["bearing"])

            elif event.get("type") == "take_failed":
                self.skipped_pickups[int(event.get("id", -1))] = time + 25.0

        if self.last_position is not None:
            movement = math.hypot(
                x - self.last_position[0],
                y - self.last_position[1],
            )

            if movement < 0.12:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
        else:
            self.stuck_ticks = 0

        self.last_position = (x, y)

        action: mf.Action = {"tick": observation["tick"]}

        if observation.get("offer"):
            action["pick"] = self._choose_upgrade(observation["offer"])  # type: ignore[arg-type]

        weapons = me.get("weapons", [None, None])
        gun = weapons[0] if len(weapons) > 0 and weapons[0] and weapons[0].get("kind") == "gun" else None

        bullet_speed = 252.0

        if gun is not None:
            bullet_speed = max(
                1.0,
                float(gun.get("stats", {}).get("speed", 252.0)),
            )

            ammo = float(gun.get("ammo", 0.0))
            magazine = float(gun.get("stats", {}).get("mag", 12.0))
            reload_left = float(gun.get("reload", 0.0))

            if reload_left <= 0.0 and ammo <= max(2.0, magazine * 0.30):
                action["reload"] = True

        pickups = observation.get("pickups", [])

        if len(weapons) > 1 and weapons[1] is None:
            for pickup in pickups:
                if pickup.get("kind") in ("laser", "lightning", "mortar"):
                    if _dist2d(pickup["pos"], [x, y]) <= self.pickup_radius:
                        action["take"] = pickup["id"]
                        break

        mechs = observation.get("mechs", [])
        enemies = [mech for mech in mechs if mech.get("kind") == "mech"]

        zone = observation["zone"]
        zone_center = zone.get("center", [0.0, 0.0])
        zone_radius = float(zone.get("radius", 500.0))
        walls = observation.get("walls", [])

        nearest_enemy_distance = min(
            (
                math.hypot(enemy["pos"][0] - x, enemy["pos"][1] - y)
                for enemy in enemies
            ),
            default=999.0,
        )

        recently_damaged = (time - self.last_damage_time) < 5.0
        low_health = hp_ratio < 0.55
        critical_health = hp_ratio < 0.30

        # Audio threat detection:
        # avoid nearby gunfights when weak, under-equipped, or already damaged.
        sounds = observation.get("sounds", [])
        nearby_gunfire = 0

        for sound in sounds:
            if sound.get("kind") in ("gun", "laser", "lightning", "mortar"):
                sound_position = sound.get("pos")

                if sound_position is not None:
                    distance = math.hypot(
                        float(sound_position[0]) - x,
                        float(sound_position[1]) - y,
                    )

                    if distance < 80.0:
                        nearby_gunfire += 1

        under_equipped = len(weapons) <= 1 or weapons[1] is None
        avoid_fights = (
            critical_health
            or (low_health and under_equipped)
            or (recently_damaged and hp_ratio < 0.70)
            or (nearby_gunfire >= 2 and under_equipped)
        )

        # Choose a vulture target:
        # weakened, distracted, isolated, or trapped by the zone.
        target: Optional[Dict[str, Any]] = None

        if enemies and not avoid_fights:
            def opportunity_score(enemy: Dict[str, Any]) -> float:
                distance = math.dist(enemy["pos"], [x, y, z])
                enemy_hp = float(enemy.get("hp", 1400.0))
                enemy_max_hp = max(1.0, float(enemy.get("max_hp", 1400.0)))
                enemy_hp_ratio = enemy_hp / enemy_max_hp

                score = distance
                score += enemy_hp_ratio * 100.0

                # Prefer enemies already fighting someone else.
                if enemy.get("target_id") is not None:
                    score -= 35.0

                # Prefer enemies close to the zone edge.
                enemy_zone_distance = math.hypot(
                    enemy["pos"][0] - zone_center[0],
                    enemy["pos"][1] - zone_center[1],
                )

                if enemy_zone_distance > zone_radius * 0.80:
                    score -= 25.0

                # Prefer enemies carrying cubes.
                cubes = int(enemy.get("cubes", 0))
                score -= cubes * 5.0

                return score

            candidates = sorted(enemies, key=opportunity_score)
            best_candidate = candidates[0]

            enemy_hp_ratio = float(best_candidate.get("hp", 1400.0)) / max(
                1.0,
                float(best_candidate.get("max_hp", 1400.0)),
            )

            enemy_zone_distance = math.hypot(
                best_candidate["pos"][0] - zone_center[0],
                best_candidate["pos"][1] - zone_center[1],
            )

            distracted = best_candidate.get("target_id") is not None
            weakened = enemy_hp_ratio < 0.60
            zone_trapped = enemy_zone_distance > zone_radius * 0.80

            # Only commit when there is a real opportunity.
            if weakened or distracted or zone_trapped:
                target = best_candidate

                if self.target_id is None or time > self.target_until:
                    self.target_id = target.get("id")
                    self.target_until = time + 3.0

                for enemy in enemies:
                    if enemy.get("id") == self.target_id:
                        target = enemy
                        break

        # Safe farming: prioritize a second weapon early, but abandon it if danger appears.
        loot_target: Optional[Dict[str, Any]] = None
        loot_distance = 0.0

        if (
            len(weapons) > 1
            and weapons[1] is None
            and in_zone
            and nearest_enemy_distance > 75.0
            and not recently_damaged
        ):
            held_weapons = {weapon.get("kind") for weapon in weapons if weapon}
            best_score = -1e9

            for pickup in pickups:
                pickup_kind = pickup.get("kind")

                if pickup_kind not in ("laser", "lightning", "mortar"):
                    continue

                if pickup_kind in held_weapons:
                    continue

                if self.skipped_pickups.get(int(pickup["id"]), -1.0) > time:
                    continue

                ground = self.terrain.ground(
                    float(pickup["pos"][0]),
                    float(pickup["pos"][1]),
                )

                if ground is not None and float(pickup["pos"][2]) - ground > 2.0:
                    continue

                distance = _dist2d(pickup["pos"], [x, y])

                if distance > 180.0:
                    continue

                value = {
                    "laser": 50.0,
                    "lightning": 35.0,
                    "mortar": 15.0,
                }[pickup_kind]

                score = value - 0.30 * distance

                if score > best_score:
                    best_score = score
                    loot_target = pickup
                    loot_distance = distance

        if loot_target is not None:
            self.loot_memory = {
                "id": int(loot_target["id"]),
                "kind": loot_target["kind"],
                "pos": [
                    float(loot_target["pos"][0]),
                    float(loot_target["pos"][1]),
                    float(loot_target["pos"][2]),
                ],
                "seen": time,
            }
        elif self.loot_memory is not None:
            memory = self.loot_memory
            memory_distance = _dist2d(memory["pos"], [x, y])

            gone = (
                len(weapons) <= 1
                or weapons[1] is not None
                or not in_zone
                or nearest_enemy_distance < 65.0
                or recently_damaged
                or time - memory["seen"] > 6.0
                or memory_distance < 35.0
                or self.skipped_pickups.get(memory["id"], -1.0) > time
            )

            if gone:
                self.loot_memory = None
            else:
                loot_target = memory
                loot_distance = memory_distance

        if loot_target is not None and self.stuck_ticks >= 10:
            self.skipped_pickups[int(loot_target["id"])] = time + 15.0
            self.loot_memory = None
            loot_target = None

        zone_x, zone_y = self._zone_target(observation, x, y)

        move_x = zone_x - x
        move_y = zone_y - y
        behavior = "zone_edge_patrol"

        my_zone_distance = math.hypot(
            x - zone_center[0],
            y - zone_center[1],
        )

        # Zone survival always overrides combat.
        if not in_zone or my_zone_distance >= zone_radius * 0.90:
            move_x = zone_x - x
            move_y = zone_y - y
            behavior = "zone_escape"

        elif critical_health and enemies:
            # Break contact and seek cover; do not trade shots while weak.
            closest_enemy = min(
                enemies,
                key=lambda enemy: math.hypot(
                    enemy["pos"][0] - x,
                    enemy["pos"][1] - y,
                ),
            )

            away_x = x - closest_enemy["pos"][0]
            away_y = y - closest_enemy["pos"][1]
            away_length = max(1e-9, math.hypot(away_x, away_y))

            move_x = (away_x / away_length) * 85.0 + (zone_x - x) * 0.60
            move_y = (away_y / away_length) * 85.0 + (zone_y - y) * 0.60
            behavior = "disengage_and_regen"

        elif avoid_fights and enemies:
            # Move away from the enemy centroid and toward the zone edge.
            centroid_x = sum(float(enemy["pos"][0]) for enemy in enemies) / len(enemies)
            centroid_y = sum(float(enemy["pos"][1]) for enemy in enemies) / len(enemies)

            away_x = x - centroid_x
            away_y = y - centroid_y
            away_length = max(1e-9, math.hypot(away_x, away_y))

            move_x = (away_x / away_length) * 70.0 + (zone_x - x) * 0.75
            move_y = (away_y / away_length) * 70.0 + (zone_y - y) * 0.75
            behavior = "avoid_brawl"

        elif target is not None:
            # Third-party cautiously: maintain range and strafe.
            target_x, target_y, target_z = target["pos"]
            target_distance = math.hypot(target_x - x, target_y - y)
            target_yaw = math.atan2(target_y - y, target_x - x)

            if self.rng.random() < 0.03 or time > self.strafe_until:
                self.strafe_direction = self.rng.choice([-1.0, 1.0])
                self.strafe_until = time + self.rng.uniform(1.5, 3.0)

            strafe_yaw = target_yaw + self.strafe_direction * (math.pi * 0.5)

            if hp_ratio > 0.70:
                desired_range = 65.0
            elif hp_ratio > 0.45:
                desired_range = 90.0
            else:
                desired_range = 115.0

            radial_pull = 0.0

            if target_distance < desired_range * 0.75:
                radial_pull = -0.55
            elif target_distance > desired_range * 1.30 and in_zone:
                radial_pull = 0.30

            move_x = (
                (zone_x - x) * 0.45
                + math.cos(target_yaw) * radial_pull * 40.0
                + math.cos(strafe_yaw) * 28.0
            )
            move_y = (
                (zone_y - y) * 0.45
                + math.sin(target_yaw) * radial_pull * 40.0
                + math.sin(strafe_yaw) * 28.0
            )

            behavior = "third_party"

        elif loot_target is not None:
            move_x = float(loot_target["pos"][0]) - x
            move_y = float(loot_target["pos"][1]) - y
            behavior = "safe_farming"

        # Anti-stuck reflex: sidestep and jump.
        if self.stuck_ticks >= 8:
            side_x = -math.sin(math.atan2(move_y, move_x))
            side_y = math.cos(math.atan2(move_y, move_x))

            move_x = side_x * 60.0 + (zone_x - x) * 0.40
            move_y = side_y * 60.0 + (zone_y - y) * 0.40
            behavior = "anti_stuck"

        move_length = math.hypot(move_x, move_y)

        if move_length > 1e-9:
            action["move"] = [
                move_x / move_length,
                move_y / move_length,
            ]
        else:
            action["move"] = [0.0, 0.0]

        should_boost = (
            (not in_zone and energy > 18.0)
            or (my_zone_distance >= zone_radius * 0.92 and energy > 22.0)
            or (critical_health and recently_damaged and energy > 30.0)
            or (
                behavior == "third_party"
                and target is not None
                and energy > 30.0
                and nearest_enemy_distance > 75.0
            )
        )

        action["boost"] = bool(should_boost)
        action["jump"] = bool(
            me.get("grounded", False)
            and (
                self.stuck_ticks >= 6
                or (behavior == "disengage_and_regen" and self.rng.random() < 0.05)
            )
        )

        if target is not None:
            target_position = target["pos"]
            target_velocity = target.get("vel", [0.0, 0.0, 0.0])
            horizontal_distance = max(
                1.0,
                math.hypot(target_position[0] - x, target_position[1] - y),
            )

            target_kind = target.get("kind")
            target_speed = 252.0

            if target_kind == "mortar":
                # Mortars need more lead and an upward arc.
                lead_time = min(1.4, horizontal_distance / 150.0)
            else:
                lead_time = min(1.0, horizontal_distance / bullet_speed)

            predicted_x = target_position[0] + target_velocity[0] * lead_time
            predicted_y = target_position[1] + target_velocity[1] * lead_time

            target_cubes = int(target.get("cubes", 0))
            predicted_z = target_position[2] + 4.5 * (
                1.0 + 0.05 * min(target_cubes, 10)
            )

            yaw = math.atan2(predicted_y - y, predicted_x - x)
            pitch = math.atan2(predicted_z - muzzle_z, horizontal_distance)

            if target_kind == "mortar":
                pitch += 0.12

            pitch = max(-0.8, min(0.8, pitch))

            action["aim"] = [yaw, pitch]

            target_weapons = target.get("weapons", [])
            target_has_mortar = "mortar" in target_weapons

            fire_primary = self._weapon_decision(
                0,
                weapons[0] if len(weapons) > 0 else None,
                horizontal_distance,
                True,
                energy,
                target_has_mortar,
            )

            fire_secondary = self._weapon_decision(
                1,
                weapons[1] if len(weapons) > 1 else None,
                horizontal_distance,
                True,
                energy,
                target_has_mortar,
            )

            fire = [fire_primary, fire_secondary]

            my_aim = me.get("aim", [yaw, pitch])
            aim_error = abs(_wrap_angle(float(my_aim[0]) - yaw))

            for index, weapon in enumerate(weapons[:2]):
                if weapon is not None and weapon.get("kind") == "laser" and self.laser_charging[index]:
                    continue

                if aim_error > 0.06:
                    fire[index] = False

            # Do not waste ammunition while disengaging.
            if behavior in ("disengage_and_regen", "avoid_brawl", "zone_escape"):
                if horizontal_distance > 90.0:
                    fire = [False, False]

            action["fire"] = fire

            mf.draw.line(
                [x, y, muzzle_z],
                [predicted_x, predicted_y, predicted_z],
                "#40ff80",
            )

        else:
            self.laser_charging = [False, False]

            if recently_damaged and self.last_damage_bearing is not None:
                action["aim"] = [_wrap_angle(self.last_damage_bearing), 0.0]
            elif loot_target is not None:
                action["aim"] = [
                    math.atan2(
                        float(loot_target["pos"][1]) - y,
                        float(loot_target["pos"][0]) - x,
                    ),
                    0.0,
                ]
            else:
                action["aim"] = [
                    math.atan2(zone_y - y, zone_x - x),
                    0.0,
                ]

            action["fire"] = [False, False]

        mf.draw.point([zone_x, zone_y, z], "#40c0ff", 6)
        mf.draw.label(
            f"VULTURE v8 | hp {hp:.0f}/{max_hp:.0f} | "
            f"mode {behavior} | enemies {len(enemies)} | "
            f"gunfire {nearby_gunfire}"
        )

        return action


mf.run(Bot)